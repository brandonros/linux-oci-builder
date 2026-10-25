> Historical development log, preserved during the repository migration.
> Entries describe successive experiments and may contradict later results.
> Local artifact paths refer to the original docker-fun workspace. See ../README.md for current usage.

# image-forge

The root `Dockerfile` is a minimal hello-world recipe using Alpine 3.22 and
`CMD ["/bin/echo", "Hello, world!"]`. The equivalent `00-hello` Blink checkpoint
passes on macOS ARM64:

```sh
cargo run --example blink-suite -- --blink ext/blink/o/blink/blink --filter 00-hello
```

An experimental Rust container-image builder for macOS, combining OCI image
assembly with Linux userspace execution through Blink, without a Docker daemon
or a booted Linux VM.

The integration targets are the Claude-base and code-server Dockerfiles from
`brandonros/agent-platform`, source commit `084da8c60f916838d866c2607a0096c87a24be89`, under
`ext/agent-platform`. Both fresh builds and OCI exports completed on macOS ARM64
using Blink, with no Linux kernel or hardware VM. Independently validated Blink
fixes are collected in [draft PR #218](https://github.com/jart/blink/pull/218).

Current artifacts (2026-10-25):

- Claude-base: `target/case-sensitive-builds/claude-base/image`, manifest
  `sha256:8c0186b4c72d3f32cfbe7dabf39140419eae2fc669531615604846c021185b6b`.
- Code-server: `target/case-sensitive-builds/code-server/image`, manifest
  `sha256:057942606cae4c65116eac292d82766fb846c27bdab0dc2d45a95add7f266bcf`.
- Both archive-only target verifiers pass. Full-byte comparisons with published
  `claude-base:5` and `code-server:24` are in `target/oci-comparison/`.
  Code-server's runtime configuration matches; shared paths have no UID/GID,
  mode, link-target or xattr differences. Remaining file differences are generated
  IDs/passwords/timestamps, logs/caches, `/etc/hosts`, a Claude backup, and ZIP
  serialization. All nine Command Center VSIX member contents match.
- Command Center's original npm commands succeeded under the interpreter after
  preserving a JIT crash. The final code-server stage succeeded under JIT.
  This does not establish general JIT correctness or complete Linux compatibility.
- `target/case-sensitive-builds/reproduce.sh` records the checkpoint commands.
  `source-context.tar` exactly matches all 44 recorded source files. Build output
  requires a case-sensitive filesystem; the mounted APFS sparse bundle is
  `target/image-forge-build.sparsebundle`. Paths inside its mount require it to
  remain mounted. The build driver rejects case-insensitive output directories.
- The runtime is experimental: placeholder proc maps and SSE4 advertisement,
  signal/epoll prototypes, and the external eventfd retrigger patch are still
  required by this build setup. Their known limits are documented below; only
  independently validated fixes are represented as upstream-ready.

The checkpoints are Debian package installation; Linux user/ownership semantics;
org configuration COPY, WORKDIR and ENV; the Claude installer; Node/npm and VSIX
packaging; the derived code-server image and extensions; and finally OCI export
and exported-image verification. The user's clarified scope is building these
images on macOS, not running the completed images or editor on macOS. Blink
runtime compatibility work is needed only for Dockerfile RUN commands and correct
image output. Earlier editor-server experiments below are historical evidence,
not completion gates. A diagnostic workaround is recorded as such and does
not count as implementing the corresponding Linux behavior.

Historical build/debug notes (superseded by the current artifacts above):

- **Build-only follow-up:** a new build of the original Claude-base Dockerfile
  is running in `target/fresh-claude-base-rootless-v3`, starting from the original
  Debian OCI base and using `--rootless-metadata` with
  `target/blink-runtimes/rootless-build-v3`. This directly addresses incorrect
  ownership in earlier exported images. Fourteen metadata checks pass in both
  modes, including build-user real/effective/saved IDs and filesystem-ID creation
  ownership (`target/ownership-experiment/vfs/build-backend-v2-validation.json`).
  A checkpoint layer export also preserves UID/GID 1000:1000, file mode 4755,
  hardlinks and symlinks (`target/rootless-build-export-check`); its symlink mode
  check exposed Darwin's 0755 default, subsequently corrected to Linux's 0777 in
  v2. The completed editor is not being tested further on macOS.
  The first seeding attempt tried to chmod virtual `/dev` and failed; it is
  preserved in `failed-001-virtual-mount-mode`. Seeding now excludes virtual
  mounts consistently with export. Base metadata seeding and the baseline
  full-rootfs snapshot pass; checkpoint `003-run` is installing the Dockerfile's
  Debian packages.
  That first rootless attempt then exposed dpkg's mode-000 creation pattern:
  Darwin rejected metadata xattr writes before host access was enabled. V3
  establishes host metadata access first while retaining the guest mode 000;
  both execution modes pass the new creation regression and the complete suite
  (`target/ownership-experiment/vfs/mode-zero-after.json`). The failed checkpoint
  remains in `target/fresh-claude-base-rootless/failed-003-mode-zero-creation`.
  Its subsequent retry stopped on the broken package state, so v3 starts from a
  clean base rather than treating a repaired partial installation as a fresh run.
  `scripts/verify-target-image.py` verifies OCI contents, ownership, modes, COPY
  content and target configuration without executing the image. The old exports
  fail its ownership check (`target/verify-old-claude-image.json` and
  `target/verify-old-code-server-image.json`), as expected.
- **Claude-base now builds and exports successfully.** The resumed fresh build
  `target/fresh-claude-base-proc-fd` ran the original bootstrap command under JIT
  with exit 0, installed Claude 2.1.310, and captured the full rootfs. Its image
  is `target/fresh-claude-base-proc-fd/image`, manifest
  `sha256:f48dda54bca54a7e05d1a91c3317014bad50398fcfc46d7e9604884116a65fa7`.
  Manifest/blob digests, sizes and every layer DiffID pass independent verification.
  Fresh-unpack checks pass `id` (1000:1000), working directory, Claude `--version`
  and `--help`. However, the installer-created binary is recorded as 0:0 rather
  than 1000:1000: creation ownership under fakeroot is not yet faithfully captured.
  This metadata defect remains a release blocker even though the CLI runs.
  `scripts/probe-creation-ownership.py` reproduces it independently of Claude:
  with guest effective IDs 1000:1000, a new file, directory, symlink and hardlink
  all report 0:0 (`target/creation-ownership-before`, exit 1). An explicitly
  recorded 0:0 owner remains 0:0. This probes fakeroot bookkeeping, not Linux
  permission enforcement. Debian fakeroot's `process_stat` defaults unknown
  inodes to 0:0; its mkdir wrapper records mode without the creating identity.
  A diagnostic libc creation interposer makes that Python probe pass, but the
  actual Claude/Bun runtime bypasses the relevant creation/stat hooks and reports
  host IDs 501:20 for newly created paths (`target/ownership-experiment/bun`).
  The interposer is not enabled in builds. Correct tracking must cover filesystem
  syscalls inside Blink, including direct-syscall callers.
  An opt-in `BLINK_ROOTLESS=1` prototype in `blink/rootless.c` now records inode
  UID/GID in host extended attributes and handles basic guest get/setuid/gid and
  filesystem ownership operations without fakeroot. Python and actual Claude/Bun
  creation probes pass with both JIT and interpreter execution; evidence is in
  `target/ownership-experiment/vfs/{python,bun}.json`. The runtime is
  `target/blink-runtimes/rootless-owner-prototype`. A subsequent
  `rootless-mode-prototype` also stores guest permission bits in extended
  attributes: the host stripped SGID even from a native chmod, so host modes
  alone could not implement guest SGID inheritance. The new guest lifetime probe
  (`scripts/probe-rootless-lifetime.py`) passes in both modes: rename/hardlink
  identity, partial chown, symlink owner versus target, an unlinked open inode,
  SGID group and directory-mode inheritance, mode 000, and persistence across
  separate Blink processes. Evidence:
  `target/ownership-experiment/vfs/lifetime-mode-persistence.json`.
  Before build integration, the anonymous pipe fstat EINVAL
  regression is fixed in `rootless-pipe-prototype`: anonymous descriptors use
  shared metadata mappings instead of inode xattrs. Duplicate descriptors and
  both pipe ends share metadata; mappings preserve updates across fork while
  each process manages its own descriptor references. Socket-pair endpoints
  remain independent. `scripts/probe-rootless-pipe.py` fails before and passes
  afterward in both modes, covering stat, chown, chmod, dup, fork, close and I/O.
  The combined suite `scripts/test-rootless-metadata.py` passes all eight runs
  (`target/ownership-experiment/vfs/combined-pipe-validation.json`).
  `rootless-groups-prototype` also passes privileged chown mode-clearing checks
  and guest-only supplementary-group tests. Chown clears setuid and executable
  setgid bits while preserving directory bits and privileged non-executable
  SGID; symlink ownership changes leave the target untouched. These expectations
  follow Linux [chown_common](https://github.com/torvalds/linux/blob/master/fs/open.c)
  and [setattr_should_drop_sgid](https://github.com/torvalds/linux/blob/master/fs/attr.c).
  Group state starts empty instead of exposing host groups, validates IDs and
  buffer sizes, sorts group lists as Linux does, isolates child changes across
  fork, and persists across guest exec. All twelve combined checks pass in
  `target/ownership-experiment/vfs/combined-groups-validation.json`.
  Basic real/effective/saved/filesystem ID bookkeeping has since been added and
  tested for build commands. General credential/capability behavior, named special
  files, descriptor metadata transfer through SCM_RIGHTS and remaining path edge
  cases are not claimed complete. This mode is metadata bookkeeping, not privilege
  isolation. It is now selected explicitly for the fresh build noted above.
- The final code-server Dockerfile stage in `target/fresh-code-server-eventfd`
  installed code-server 4.141.0 successfully, then its first `003-run` failed
  removing the bundled Copilot directory (`rm: fts_read failed: No such file or
  directory`). A separate retry removed the remaining directory successfully;
  that does not establish the cause or complete the original build. Focused
  syscall evidence is under `target/code-server-rm-trace`; a cached-package
  reinstall and full removal reproduction is under `target/code-server-rm-full-trace`.
  The full reproduction identified incorrect ancestry after opening `.` through
  hostfs: repeated parent-directory opens returned the same inode. Blink commit
  `5baf584` fixes this and adds `tool/open_parent_probe.c`; both JIT and interpreter
  modes pass in an independent clean build. The original `003-run` now exits 0
  with `target/blink-runtimes/open-parent-diagnostic`, and the build has advanced
  to `005-run` (extension installation). The original failure is preserved in
  `failed-003-parent-traversal`; before/after probe results are under
  `target/blink-experiment/open-parent-{before,after}.json`.
  Both extensions installed successfully. The first `005-run` exited 0 but was
  rejected because worker cleanup had not verified termination; its evidence is
  in `failed-005-unverified-worker-cleanup`. Cleanup now records matching runtime
  processes and rescans until they are gone (bounded to five seconds), failing
  if termination cannot be verified. A detached guest `sleep` regression passes
  in `target/checkpoint-worker-cleanup`. The repeated `005-run` and
  `006-snapshot-after` both exit 0; final OCI assembly completed successfully.
  The image is `target/fresh-code-server-eventfd/image`, manifest
  `sha256:1a7de84dc02a80ffcae87de4778772b1114d56eacca2665ab452e675e13b7386`.
  It retains the creation-ownership defect above. Independent manifest/blob/DiffID
  verification and fresh-unpack CLI/extension/settings checks pass under
  `target/code-server-export-runtime` (runtime exit 0): Claude 2.1.310,
  code-server 4.141.0 / Code 1.141.0, anthropic.claude-code 2.1.310 and
  org.command-center 0.1.0. The unmodified entrypoint starts with its stock timeout;
  health, login and authenticated workbench HTML return HTTP 200, recorded in
  `target/code-server-export-stock-server/http-check.json`. This was a bounded
  180-second server check, terminated by its runner deadline, not a graceful
  shutdown test. Runtime logs report unsupported fs.watch/inotify and network
  interface enumeration (`uv_interface_addresses`, errno 97).
  A separate one-hour manual-test server previously ran on `http://127.0.0.1:18880`, with
  evidence in `target/code-server-manual-server` and password in the fresh root's
  `home/coder/.config/code-server/config.yaml`. No browser automation was used;
  interactive editor behavior remains unverified and is outside the clarified
  build-only scope. The server was stopped after that clarification. The separate
  combined snapshot runner still rejects snapshots taken before worker cleanup.
  The build uses the exported Claude-base image and completed Command Center
  stage. Fresh-unpack Claude CLI evidence is under
  `target/claude-base-export-runtime` (exit 0, with the ownership defect above).

- `target/fresh-command-center-poll-ready/image` is a completed OCI image from
  the actual command-center Dockerfile stage: ordinary `npm ci` and
  `npm run package`, followed by full-rootfs snapshot/diff export. Blob sizes,
  SHA-256 digests and uncompressed layer DiffIDs pass verification.
- A fresh unpack of that exported image runs Node 22.23.4 and evaluates JavaScript
  successfully. The VSIX digest is
  `45634cd8014f2a1b88a3658e5e9ebe6362347b80f4b72b4ce67e4a4900ec22fc`.
  Imported `/usr/bin/passwd` retains mode 4755 and `/usr/bin/chage` retains
  mode 2755 and owner 0:42. Evidence is in
  `target/command-center-export-runtime/runtime/{result.json,stdout}`.
- `target/fresh-claude-base-poll-ready` has completed package installation,
  user creation and org-file copies. The original Claude bootstrap command failed
  under JIT with exit 139. Fix `5adc734` corrects the fast-arithmetic carry bug
  responsible for its invalid countdown index. Both the patched JIT installer
  and completed interpreter bootstrap now reach a filesystem error: the working
  directory opens successfully, but `/proc/self/fd/<n>` readlink returns ENOENT.
  A diagnostic named-descriptor readlink implementation now passes open/dup/close
  and short-buffer checks in both modes and lets Claude reach native installation.
  The first version returned stale paths after rename/unlink. The macOS backend
  now uses native descriptor paths plus identity checks; the lifetime probe
  confirms `/after` following rename and `/after (deleted)` following unlink
  in both modes. Evidence: `target/blink-experiment/proc-fd-lifetime-validation.json`.
  Descriptor enumeration and rewind also pass in both modes. These tests pass
  independently in the clean VFS build; scoped named-descriptor support is now
  committed as `9847cc4` for draft PR #218. Anonymous descriptor target strings
  remain unsupported, and non-macOS hostfs retains cached-path limitations.
  The fresh build under `target/fresh-claude-base-proc-fd` completed packages,
  user creation and copies, but its native installer then reported a download
  deadline and exited 137 after about 679 seconds. The outer runner did not
  time out. A zero-byte version placeholder is not an installed Claude binary.
  Exact-runtime Bun probes isolated the cause: the epoll-shim eventfd backend
  suppressed repeated EPOLLET wakeups while the counter remained nonzero.
  Refreshing its kqueue notification on each write changes the 16 MiB
  response-to-file probe from a 120-second timeout to 936 ms. The repeated-write
  regression fails before and passes after in both modes. The original
  bootstrap retry with `eventfd-retrigger-diagnostic` subsequently exited 0.
  The shim patch is kept in `ext/blink/tool/epoll-shim-eventfd-retrigger.patch`
  against epoll-shim commit `18159584bb3d17e601b9315a7398ace018251bdc`.
  It does not fix inherited eventfd descriptors across fork.
  Claude-base export is now complete as recorded above; the final code-server
  image remains unfinished.
- Readiness (`b19c6f4`), fchmodat2 (`c25559a`) and Darwin O_PATH directory
  handling (`3120c1f`) fixes have isolated regressions and are pushed to draft
  Blink PR #218. Signal/epoll changes remain experimental and require additional
  correctness checks before upstream commits. The forked eventfd probe confirms
  the epoll-shim limitation: inherited eventfd writes fail with EBADF in both
  modes (`target/blink-experiment/eventfd-fork-limitation.json`).
- Node HTTPS fetch exposed missing Darwin TCP_KEEPIDLE translation. Fix
  `c7db4a9` maps it to TCP_KEEPALIVE; set/get regression tests fail before and
  pass after in both modes in the clean build. Node then fetches a 1 MiB range
  successfully in 3.528 seconds from the exported Command Center image.
- Runtime binaries are immutable per checkpoint. The fresh procfs build uses
  `target/blink-runtimes/proc-fd-diagnostic`; subsequent isolated checks use
  `target/blink-runtimes/keepidle-diagnostic`. Both include
  incomplete procfs maps and SSE4 feature advertising. Successful checkpoints
  do not establish complete Linux compatibility. Browser automation is stopped;
  final editor interaction will be tested manually after the image is ready.

The entries below describe earlier diagnostic work in a manually assembled rootfs;
its installed applications are not evidence of a completed fresh target build.

Opt-in JIT fault diagnostics:

```sh
python3 scripts/build-blink-diagnostic.py --make /path/to/gmake \
  --output target/blink-runtimes/my-fault-trace
# Add these options to a separate blink-checkpoint.py reproduction:
# --jit --fault-trace --trace
```

`--fault-trace` sets host `BLINK_TRACE_FAULTS=1`. The runtime keeps a bounded
64-entry history of guest dispatch IP/SP/BP and logs `FAULTTRACE` records to
that checkpoint's `blink.log` before delivering a synchronous guest fault.
It includes all 16 general registers, flags, guest fault address and the native
fault PC on macOS ARM64 (zero for software-generated guest faults). These are
dispatch boundaries, not every instruction inside compiled JIT paths. Native
signal handling only captures the PC; trace formatting happens after unwinding.
`--trace` separately enables syscall logging. Fault tracing is disabled by default.
A deliberate null-write probe in `ext/blink/tool/fault_trace_probe.S` verified
that tracing preserves SIGSEGV, records a known R12 value, and emits no
`FAULTTRACE` records when disabled, in both execution modes.

Historical target checkpoints (2026-10-24):

- Debian packages, the coder account, org files, and code-server 4.141.0 are present
  in the experimental rootfs. This is a manually assembled checkpoint.
- The command-center stage completed ordinary `npm run package` twice, without
  Node GC or optimization overrides. The interrupted VSIX
  reinstall left a dangling registry entry; that state was backed up under
  `/tmp/command-center-interrupted-backup` in the guest and cleared. A clean
  install then exited 0. The final directory and registry agree, and all six
  extension payload files match the VSIX (allowing installer metadata in
  package.json). Both Anthropic Claude Code and Command Center are registered.
- Claude 2.1.310 downloaded with a verified digest. Its cached native installer
  now reports success and exits 0 after shutdown fix `7b2c044`. The installed
  launcher passes `--version` and `--help`, also with exit status 0. The exact
  Dockerfile curl/bootstrap command subsequently exited 0 in 841.8 seconds,
  including a new release-binary download in this existing rootfs.
- Code-server runtime startup exposed overlapping JIT code allocations across
  process images. Fix `312197e` and a host-side regression are in the Blink draft
  PR: the probe fails before the fix and passes after it, including pool reuse
  and shutdown/reinitialization. Unmodified startup still reaches a parent/child
  handshake timeout; diagnostic HTTP startup is described below. Signal-wait
  and epoll support remain experimental.
  A focused Node fork/IPC exchange passes. The subsequent URL/Uint32 property
  corruption was traced to JIT MOVSD stores reserving sixteen readable bytes
  instead of eight writable bytes. Fix `21e5c50` passes the page-boundary
  regression in both modes and the Node URL/Uint32 reproducers under JIT.
  Package resolution now passes. An untraced package import hit SIGSEGV, while
  subsequent traced isolated and sequential imports passed; that crash remains
  unexplained. A related CMPXCHG writable-reservation fix (`de6e25d`) passes
  locked/unlocked page-crossing regression tests in both execution modes.
- Code-server now serves HTTP 200 for `/healthz` and the workspace root HTML
  page through Blink. This diagnostic run uses a 120-second handshake deadline
  instead of the stock 10 seconds, native fault tracing, and the experimental
  signal/epoll support. One child startup took about 46 seconds. Browser/editor
  interaction and extension execution are not yet verified.
- Claude currently requires diagnostic `/proc/self/maps` and SSE4 feature
  advertising, plus the experimental epoll backend. These are not finished
  compatibility implementations. A fresh end-to-end Dockerfile build, ownership
  validation, OCI export, and final image runtime checks remain outstanding.

Currently, the CLI pulls public images into an OCI layout. Separate Blink examples
execute Linux programs and a restricted Dockerfile subset against extracted Alpine
filesystems. The `oci-assemble` example can now assemble a derived layout from
a prepared layer and runtime configuration. `scripts/oci-diff.py` generates
changesets from tar snapshots, and guest tar capture preserves tested ownership
and modes under fakeroot. `scripts/build-targets.py` now drives the target Dockerfile stages with resumable
per-instruction checkpoints, full-stage capture and OCI assembly.
`scripts/run-image.py` verifies and unpacks an exported image for Blink runtime
checks. The complete Claude-base/code-server pipeline remains unfinished.

The CLI defaults to `docker.io/library/alpine:3.22`, matching the example
Dockerfiles. It downloads the base image; it does not yet interpret a Dockerfile
or apply its `CMD` instruction.

```sh
cargo run --
cargo run -- docker.io/library/alpine:3.22 --platform linux/amd64 --output alpine-amd64
```

The default platform is Linux with the host CPU architecture (`arm64` on Apple
Silicon, `amd64` on x86-64). Use `--platform OS/architecture[/variant]` to choose
another platform. Tag and `@sha256:...` references are supported.

The destination must not exist. Downloads are staged in a temporary directory
and published only after all digests and sizes have been verified.

```text
image/
  oci-layout       # OCI layout version
  index.json       # Points to the selected platform's manifest
  blobs/sha256/
    <digest>      # Original manifest JSON
    <digest>      # Image configuration JSON
    <digest>      # Layer bytes, still compressed as served by the registry
```

The manifest's `config` and ordered `layers` descriptors identify each blob's
digest, size, and media type. Filenames have no extension because they are
content hashes. Manifest bytes and media types are preserved, including Docker
schema 2 manifests when supplied by the registry. The generated OCI layout
contains only the selected image, not every platform or attached attestation.

Registry authentication for public pulls is handled by
[oci-client](https://docs.rs/oci-client/0.18.0/oci_client/). The pull CLI supports
SHA-256 blobs and anonymous access; it does not read Docker credentials or execute
the image. Linux execution is available through the Blink experiment below.
Docker Hub may rate-limit anonymous pulls.

```sh
cargo test
cargo clippy --all-targets -- -D warnings
```

## Assembling derived OCI images

`oci-assemble` takes a single-image layout from the pull CLI and optional runtime
configuration and filesystem changes. A config-only change adds history without
inventing a filesystem layer:

```sh
printf '%s\n' '{"Cmd":["echo","Hello, world!"]}' > /tmp/hello-config.json
cargo run --example oci-assemble -- \
  --base target/blink-experiment/image \
  --config /tmp/hello-config.json \
  --output target/blink-experiment/hello-image
```

Add `--layer path/to/change.tar` for a prepared **uncompressed** OCI changeset.
Its ownership, modes, symlinks, and whiteout entries must already be correct;
this command neither snapshots a rootfs nor generates deletion whiteouts. It
validates the archive with `bsdtar`, copies and verifies the base's stored blobs,
appends the new uncompressed DiffID, and writes config, manifest and index with
new digests. Existing base DiffIDs are inherited from its verified config;
compressed base layers are not decompressed by this command to recheck DiffIDs.
Runtime configuration fields in `--config` replace fields in the inherited OCI
`config` object; arrays such as Env and Cmd are replaced whole. The base layout
is unchanged, and the destination must not exist. No registry push is performed.

A real Alpine-derived output is retained at
`target/blink-experiment/assembled-hello`. Independent verification checked all
blob sizes/digests, every uncompressed DiffID, and UID/GID 1000 on its new
`hello.txt` entry. After unpacking its layers, executing the command from its
OCI config through Blink printed `Hello from an assembled OCI image!` and exited
0. This is an assembly/runtime smoke check, not either target Dockerfile build.
The implementation follows the [OCI configuration specification](https://github.com/opencontainers/image-spec/blob/main/config.md)
for DiffIDs and history. Tests cover layer assembly, config-only history, base
immutability, existing-output rejection, and corrupt-base rejection before
publication. `cargo test --all-targets` and Clippy pass.

## Blink execution experiment

We successfully ran Alpine's **AMD64 Linux** shell, `cat`, `uname`, a child
shell, and `apk --version` on macOS ARM64 using [Blink](https://github.com/jart/blink).
The shell created `/hello.txt` inside the extracted filesystem. No Linux kernel
or hardware VM was started. The original smoke test does not build a new OCI
image. The suite below also exercises Dockerfile `RUN` instructions, including
successful package installation from Alpine's HTTPS repositories.

Reproduce with a C compiler, GNU Make 4+, `bsdtar`, and Rust:

```sh
git clone https://github.com/jart/blink.git /tmp/docker-fun-blink
git -C /tmp/docker-fun-blink checkout f006a4fc6f9b8de9272504fdff0dbbe5ce5dc580
(cd /tmp/docker-fun-blink && ./configure --enable-vfs && gmake -j4 o//blink/blink)
mkdir -p target/blink-experiment/bin
cp /tmp/docker-fun-blink/o/blink/blink target/blink-experiment/bin/blink
cargo run -- --platform linux/amd64 --output target/blink-experiment/image
cargo run --example blink-smoke
```

Skip the clone/build/pull steps if those artifacts already exist. On this machine
GNU Make was supplied through `nix shell nixpkgs#gnumake -c make -j4 o//blink/blink`
from Blink's source directory. The double slash in that Make target is intentional.

The Rust examples validate the blobs, apply layers in manifest order with
`bsdtar`, invokes Blink as a subprocess, and checks the guest-created file.
Each run saves its filesystem under `target/blink-experiment/run-*`.
Use `--blink PATH --image PATH` to override the defaults.

Blink needs `-m` for this Alpine binary's page alignment on Apple Silicon; this
disables its linear-memory optimization. The VFS root is **not a security
sandbox**: Blink exposes host resources, including a host-root mount. Run only
trusted workloads. This smoke test does not preserve Linux ownership for image
export, handle whiteouts, or establish general Dockerfile compatibility.

## Dockerfile experiment suite

```sh
cargo run --example blink-suite
cargo run --example blink-suite -- --filter 03-script
```

The runner reads the recipes in `examples/dockerfiles/`. Each gets a fresh
filesystem; its `RUN` steps share that filesystem but execute in separate
processes. After the build steps it executes the last `CMD` as a runtime smoke
check (a normal image build would only store `CMD`).

| Dockerfile | What it exercises | Observed result on macOS ARM64 |
| --- | --- | --- |
| `00-hello.Dockerfile` | Run the hello-world command from the base image | Pass |
| `01-files.Dockerfile` | Copy, rename, hard links, symlinks, chmod, deletion | Pass |
| `02-pipelines.Dockerfile` | Pipes, sorting, command substitution, exit status | Pass |
| `03-script.Dockerfile` | Generated executable script, shebang, arguments | Pass |
| `04-archives.Dockerfile` | Tar/gzip round-trip, comparison, SHA-256 | Pass |
| `05-environment.Dockerfile` | Files persist; shell exports reset between steps | Pass |
| `06-apk.Dockerfile` | HTTPS package indexes, install curl/dependencies, trigger scripts, run curl | Pass |
| `07-proc.Dockerfile` | Read Linux `/proc/self/status` | Fails: file unavailable in this Blink build |

The default suite intentionally includes the failing `/proc` compatibility probe
and exits nonzero when a recipe fails. It continues testing the other recipes and
writes the latest invocation's results to `target/blink-experiment/suite-results.json`.
Each case prints its retained
`run-*` directory containing the rootfs, stdout, stderr, and Blink logs per step.
Steps have a 30-second timeout; timed-out process groups are killed.

The runner supports a `FROM` matching the supplied OCI layout's source reference, single-line shell-form `RUN`
without flags, and JSON-array `CMD` with an absolute executable. Unsupported
instructions fail explicitly. It does not yet support `COPY`, `ENV`, `WORKDIR`,
multi-stage builds, general layer extraction, or OCI image export.

The optional `--network` flag includes the package test and supplies host DNS
configuration to the guest. It is a test-selection flag, not a network sandbox.
Package scripts execute with Blink's host access. The package experiment passed
after explicit approval: nine packages installed and `curl --version` succeeded.

```sh
cargo run --example blink-suite -- --network --filter 06-apk --timeout 60
cargo test --all-targets
```

## Debian / Claude-base stress-test checkpoint

The first checkpoint from the proposed Claude Code / code-server build is saved
in `examples/dockerfiles/debian/`. These are execution probes, not a completed
multi-stage builder. The org configuration and extension sources are now available
under `ext/agent-platform`; a fresh full application build remains outstanding.

```sh
cargo run -- docker.io/library/debian:trixie-slim --platform linux/amd64 --output target/blink-experiment/debian-image
cargo run --example blink-suite -- --image target/blink-experiment/debian-image --recipes examples/dockerfiles/debian --network --timeout 180 --report target/blink-experiment/debian-results.json
```

Skip the pull if the destination already exists. `--recipes` chooses a recipe
directory; `--image` must match its `FROM`. `--no-jit` disables Blink's JIT for
diagnostics. `--report` keeps separate result files for different experiments.

Observed with Blink `f006a4fc6f9b8de9272504fdff0dbbe5ce5dc580` and Debian manifest
`sha256:4876fdea29cac1a33a3195f1eb1952feb1fca7dd6dd00be3e2f1879cf20d34d2`:

| Probe | Result |
| --- | --- |
| `00-shell.Dockerfile`: Bash, libc version, identity | Bash aborts with `malloc(): corrupted top size` |
| `01-packages.Dockerfile`: exact requested apt installation | `apt-get update` exits 100; HTTP helpers abort with the same allocator error, before packages install |
| `02-user.Dockerfile`: create `coder` | `useradd` exits 1: `Cannot open audit interface - aborting.` |
| `03-basics.Dockerfile`: true, dash, apt version | Pass |

Direct checks of Bash and `id` also abort with JIT disabled, while `true`, `dash`,
and `apt-get --version` pass in both modes. This narrows the issue to compatibility
in this Blink setup; it does not establish the underlying cause of the allocator
failure. The next step toward this workload is diagnosing those runtime failures,
before testing Claude Code, Node, or code-server. No host root privileges or
installer workarounds were used to bypass these failures.

### Local Blink fork: heap page-size regression

`ext/blink` now uses `git@github.com:brandonros/blink.git` as `origin` and
`git@github.com:jart/blink.git` as `upstream`. Changes are local, not pushed.

The allocator crash above was isolated to `SysBrk`: in non-linear (`-m`) mode
Blink advertises 4096-byte guest pages but rounded heap growth to the host's
16384-byte pages. The trace showed a request for `0x110001169000` returning
`0x11000116c000`. The local fix uses the guest page size in that mode.

`ext/blink/test/func/brk_pagesize_test.c` reads `AT_PAGESZ`, requests one guest
page of heap growth, restores the original break, and checks the returned address.
Before the fix it passes in linear mode but exits 2 with `-m` and `-jm` on Apple
Silicon. After the fix it passes all modes in Blink's standard function-test harness.

```sh
cd ext/blink
./configure --disable-vfs MODE=opt
gmake -j4 MODE=opt o/opt/test/func/brk_pagesize_test.com.ok
# Build the VFS-enabled executable used for Debian filesystem tests:
./configure --enable-vfs
gmake -j4 o//blink/blink
```

The VFS-disabled build above allows the test cross-compiler to access host build
paths without attempting to create a `/SystemRoot` mount directory on macOS's
read-only root. It tests the same `SysBrk` implementation.

The patched VFS binary at `ext/blink/o/blink/blink` passes Debian Bash and `id`
with and without JIT, and the complete `00-shell.Dockerfile` probe passes.
Use `--blink ext/blink/o/blink/blink` when running the example suite; the earlier
binary in `target/blink-experiment/bin/` is still the unmodified build.

The suite also accepts `--dns-tcp`, which supplies glibc's `RES_OPTIONS=use-vc`.
Debian `getent` resolves names in both Blink modes with this option. This permits
further package-manager experiments while the missing UDP IP_RECVERR behavior
remains unresolved; it is not a fix for default DNS compatibility.

The fork/JIT fix is committed in Blink as `6e7f52c`: a clean control build showed
SIGBUS in 5/30 JIT APT probes, versus 0/30 after restoring executable JIT access
in the child. Interpreter probes passed 30/30 in both samples. This is evidence
for the observed intermittent fault, not a guarantee against every SIGBUS.

APT then exposed two further problems:

- Cache growth originally failed because `mremap` was an ENOMEM stub. Blink
  commit `553151e` now supports non-linear private anonymous data mappings,
  including in-place growth and relocation. `apt-cache gencaches` passes with
  its default cache size. File-backed/shared, executable and linear mappings
  remain unsupported. The old `04-packages-large-cache.Dockerfile` is retained
  as a diagnostic comparison; it is no longer needed for this cache allocation.
- The disk-space check bypassed VFS path and descriptor translation. Blink commit
  `af2b699` routes `statfs` and `fstatfs` through VFS. Its regression test fails on
  the old binary and passes in both modes after the fix.

With TCP DNS, a larger cache, and those patches, APT downloaded all 50 package
archives (32.4 MB) for the exact requested package set. Installation then stopped
at `dpkg: requested operation requires superuser privilege`. Blink passes through
host user IDs; the builder needs guest identity and ownership semantics before
this is a faithful root build. APT also reported `unlockpt` EINVAL. The later fakeroot checkpoint below completes package installation; neither target
image is complete yet.

The populated diagnostic rootfs is `target/blink-experiment/run-Bb8jmS/rootfs`.
The untouched missing-index rootfs used by the SIGBUS probe is `run-QXUtuq/rootfs`;
keep those separate. Detailed probe instructions and evidence are in
`ext/blink/tool/debian_probe.md`. Both target Dockerfiles and the extension source
are available on the checked-out `command-center-extension` branch.

Further package checkpoints use Debian `fakeroot-tcp` 1.37.1.1-1, extracted with
`libfakeroot` into the disposable guest root. It reports guest UID 0 and saves
virtual ownership through `-i /tmp/fakeroot.state -s /tmp/fakeroot.state`, while
host ownership stays with the macOS user. This does not implement kernel
credentials or permission enforcement. Metadata still needs integration with
OCI layer export.

That path exposed three additional Blink bugs, now regression-tested and committed:

- `876a294`: apply timestamps with `AT_SYMLINK_NOFOLLOW`; previously VFS skipped
  the backend and returned stale errno, breaking dpkg unpacking.
- `90da5df`: parse spaces/tabs around shebang interpreters and arguments. Debian
  scripts using `#! /bin/sh` previously fell back to the host shell.
- `f442177`: initialize the ordinary ELF entry finalizer register to zero.
  Debian's static-PIE `ldconfig` previously tried to call a pathname as code.
  Both `ldconfig --version` and `ldconfig -r /` now pass in both modes;
  Cosmopolitan bootstrap Make still passes as well.

The separate Node checkpoint is in `examples/dockerfiles/node/00-runtime.Dockerfile`:

```sh
cargo run -- docker.io/library/node:22-bookworm-slim --platform linux/amd64 --output target/blink-experiment/node-image
cargo run --example blink-suite -- --blink ext/blink/o/blink/blink --image target/blink-experiment/node-image --recipes examples/dockerfiles/node --timeout 60 --report target/blink-experiment/node-runtime.json
```

The tested Node manifest is
`sha256:ea11b74b986d520137cdef9a5346e447a00f275ecc4abc0055b7151226231e09`.
Its five layers have no whiteouts. Node reports v22.23.4, but executing JavaScript
aborts during libuv initialization because macOS Blink lacks `epoll_create1`.
`ext/blink/tool/epoll_probe.c` reproduces that missing prerequisite independently.
An uncommitted epoll-shim/eventfd adapter now passes focused readiness/counter
probes and gets Node past event-loop initialization. Node still crashes during
V8 bootstrap, including in interpreter mode; a diagnostic backtrace reaches
`Builtins_InterpreterEntryTrampoline` before jumping into non-executable heap
memory. This is not yet a diagnosed instruction bug. epoll-shim also has fork
and descriptor-sharing limitations, so the prototype is not in the draft PR.

Latest Debian checkpoints:

- APT installation/configuration exits 0; git 2.47.3, curl 8.14.1, Python 3.13.5
  and dumb-init 1.2.5 report their versions.
- `a0a83f9` rejects empty VFS stat paths, fixing certificate-hook selection.
  Explicit `fstatat(AT_EMPTY_PATH)` still works.
- `c20eb0c` returns EAFNOSUPPORT for unsupported socket families, allowing
  `useradd --uid 1000 --create-home --shell /bin/bash coder` to succeed without
  kernel auditing. The saved fakeroot metadata reports home ownership 1000:1000.
- The org config, agents and skills are copied from the actual target context.
  A Python setuid/setgid/exec probe under fakeroot reports coder identity.
- `c917b23` implements match-all futex bitset waits with absolute deadlines;
  selective masks remain unsupported. Regression tests pass in both modes.
- Guest curl fails the Claude installer download with TLS 1.3 (bad record MAC),
  with JIT, interpreter and generic OpenSSL CPU paths. Explicit TLS 1.2 succeeds
  with certificate verification enabled. The exact Dockerfile command still
  fails; this is a diagnostic protocol workaround, not a TLS implementation fix.
  With a temporary guest CURL_HOME configuration limiting TLS to 1.2, the
  installer downloads and verifies Claude Code 2.1.310, then exits 132: Blink
  does not implement PINSRQ (`66 49 0f 3a 22 c0 00`, guest PC 0x267a1c8).
  Logs: `/private/tmp/blink-claude-tls12.{stdout,stderr,log}`. The installer removes
  its downloaded binary after this failure; no working Claude install exists.

Neither full target image is complete. These are rootfs checkpoints, not yet an
exportable OCI build with correct metadata and all Dockerfile semantics.

The default `ext/blink/o/blink/blink` binary is currently rebuilt without
epoll-shim; the three latest regression tests also pass in this configuration,
in both `-m` and `-jm`. The saved `blink-epoll-experiment` and
`blink-node-diagnostic` binaries retain the experimental backend. These saved
binaries include temporary diagnostic output; the instrumentation was restored
out of the tracked source before the default rebuild. Avoid rebuilding a Blink
executable while a checkpoint using that same path is active.

Claude Code 2.1.310 now passes the initial PINSRQ instruction with Blink commit
`33b8420` (PINSRD/PINSRQ register and memory lane insertion). The assembly
regression fails with SIGILL before the patch and passes both modes afterward.
The actual installer then exits 134 at Bun's explicit SSE4.2 CPU requirement.
Blink still correctly leaves its SSE4.1/SSE4.2 CPUID bits clear; broad instruction
coverage remains unfinished. A checksum-verified executable is retained in the
guest at `/tmp/claude-verified-linux-x64` for further tests. Installation is not
complete. The working curl downloader also requires the experimental eventfd
backend; a build without it reports `getaddrinfo() thread failed to start`.

Latest installer artifacts: `/private/tmp/blink-claude-pinsrdq-shim.*`.
The immutable `ext/blink/o/blink/blink-pinsrdq-shim` binary contains the
instruction fix and experimental event backend, without temporary tracing.

Blink commit `1bb0a13` fixes `POP` memory destination ordering. V8 uses
`popq 0x98(%rsp)` to rearrange a return address; Blink used the old RSP when
computing the destination, leaving a heap pointer in the return slot. The new
regression fails before the fix and passes afterward for 16/64-bit pops and
scaled-index addressing. With the experimental event backend, Node now executes
`console.log(42)` and exits 0 in both modes. Default curl TLS still fails.
The immutable runtime for this checkpoint is `ext/blink/o/blink/blink-pop-fixed`.
The actual command-center `npm ci` attempt logs to `/private/tmp/node-npm-ci.*`.

The first npm ci attempt with Blink JIT fails in a background V8 compiler
thread. A smaller `npm --version` reproduces an assertion at `blink/jit.c:704`
(`dll_is_empty(jb->jumps)`) during forced block retirement; interpreter mode
prints npm 10.9.9 and exits 0. The real npm ci retry runs with `-jm` and logs to
`/private/tmp/node-npm-ci-interp.*`; its process is bounded by a 900-second timeout.
At the last inspection it was still consuming a CPU, loading the npm dependency
workload, so package installation has not yet been claimed successful.
The default Blink build is now back on the experimental epoll-shim configuration
with the two new instruction fixes; temporary ring tracing is absent from source
and from the rebuilt default binary. Saved diagnostic binaries remain separate.

Blink `359c4ae` protects leased JIT blocks from forced retirement, including
FlushJit's temporary lease. The native `tool/jit_lease_probe.c` holds a block
with a fixup while exhausting the pool repeatedly: old code aborts at jit.c:704,
patched code exits 0. npm still reaches a separate JIT SIGSEGV; this fix does
not claim general JIT stability.

The curl TLS failure is narrowed further: TLS 1.3 succeeds with
`--curves X25519` and `--curves P-256`; `--curves X25519MLKEM768` reproduces the
bad-record-MAC error. Node HTTPS to registry.npmjs.org works with TLS 1.2
(status 200, complete response); a forced TLS 1.3 attempt receives a server
protocol-version alert. Neither test disables certificate verification.

ML-KEM now has a local reproducer, independent of TLS: guest OpenSSL 3.5.7
`genpkey -algorithm ML-KEM-768`, public-key export, `pkeyutl -encap`, and
`pkeyutl -decap` produce unequal shared secrets in both Blink modes.
A native ARM64 OpenSSL 3.5.9 control roundtrip succeeds. Native and guest
public-key derivation from the same key agree, but guest decapsulation of
native ciphertext and native decapsulation of guest ciphertext both fail to
agree on the secret. This narrows the investigation to the ML-KEM computation
path; the exact instruction or library issue is not yet identified. The native
control uses a different OpenSSL patch version, so it is not an exact build
comparison. Temporary test keys/data are under the guest `/tmp/mlkem-probe`;
only equality results are reported. SHA3-256 and SHA3-512 of `abc` match Python's
hashlib reference under the interpreter.

The command-center stage's exact `npm ci --no-audit --no-fund --ignore-scripts`
command now exits 0 under Blink `-jm`, adding 290 packages in about ten minutes.
The real extension source has been copied into the guest `/src`; `npm run
package` is the current checkpoint (`/private/tmp/node-npm-package.*`). No VSIX
or completed image is claimed until that step finishes and its output is checked.
A separate npm-version diagnostic with Node `--single-threaded` also succeeds
using Blink JIT; Node `--jitless` exits 1. The packaging checkpoint retains normal
Node settings and uses Blink interpreter mode.

An exact-version native control is now built at
`ext/openssl-3.5.7/apps/openssl` (upstream tag openssl-3.5.7, commit
8cf17aaeb4599f8af87fefd810b5b5fee90fe69e, ARM64, no-shared/no-tests).
It passes the ML-KEM-768 roundtrip and correctly decapsulates native 3.5.9
ciphertext. The OpenSSL patch-version difference therefore does not explain the
guest failure. The precise guest execution defect remains under investigation.

Latest checkpoint: Blink `fffc67a` fixes PEXTRW zero extension. The old
instruction retained stale upper register bits, corrupting OpenSSL ML-KEM's
final message addition. The regression fails before the fix and passes in both
modes. Deterministic ML-KEM-768 ciphertext now matches native ARM64 OpenSSL
3.5.7 byte for byte, and guest decapsulation agrees with the native secret in
both modes. The original `curl -fsSL https://claude.ai/install.sh` download
succeeds with default TLS negotiation and certificate verification. A separate
forced hybrid-only request received a handshake alert; it is not counted as a
successful negotiation test. The immutable runtime is
`ext/blink/o/blink/blink-pextrw-fixed`.

The command-center `npm run package` checkpoint has finished unsuccessfully:
normal Node settings abort in V8's Typer with an `UpdateType` error, even with
Blink's interpreter. A diagnostic `node --no-opt` wrapper gets TypeScript
compilation through successfully and reaches VSIX file enumeration, then the
secret scanner rejects concurrency 0 from `os.cpus().length`. Logs are in
`/private/tmp/node-npm-package-noopt-wrapper.*`. No completed VSIX or image is
claimed. These results supersede the earlier in-progress packaging and
unresolved ML-KEM notes above.

Blink `e506751` adds CPU accounting through VFS `/proc/stat`, using Mach's
per-CPU statistics on macOS and native proc data on Linux. The focused probe
passes CPU rows, small reads, pread, EOF and rewind in both modes. Node's
`os.cpus()` now reports 10 CPUs instead of zero on this host (model/frequency
remain unavailable). A packaging-only diagnostic using `--no-opt` is running
with this fix: `/private/tmp/node-vsce-procstat.*`.

Blink `54050cd` corrects UNPCKHPD: its lower result lane must come from the
old destination's upper lane. The regression fails before and passes after in
both modes, covering registers, memory and aliasing. The normal-optimization
packaging retry after PEXTRW still failed: V8 `Scavenger::Process` faults at
0x1512f67 with address 0x20000000a, then Blink kills the process while stopping
threads (exit 137). A TypeScript `--noEmit` retry with UNPCKHPD fixed is running
in `/private/tmp/node-tsc-unpckhpd.*`. Its result is not yet known.

The CPU-patched no-opt VSCE diagnostic has now exited 0. Its artifact is
`target/blink-experiment/run-x5LPlG/rootfs/tmp/command-center-procstat.vsix`
(command-center 0.1.0, 8 files, 9.16 KB), SHA-256
`041775879015cbe60545e7e305f4d721408623e1ea2977626a5775a35b2ea82e`.
ZIP integrity passes, package identity matches the repository, and all four
packaged JavaScript files equal the prior guest no-opt compilation outputs.
This completes that diagnostic packaging checkpoint, not the exact Dockerfile
command with normal optimization and not either target image.

Blink `4b2fbae` adds the four SSE4.2 packed-string comparisons, with 33
hand-derived test vectors covering controls, flags, implicit/explicit lengths,
32/64-bit length saturation, memory and aliasing. `4a671a6` adds all twelve
SSE4.1 signed/unsigned widening moves, with register/alias/guard-page tests.
Both test programs fail on the old runtime and pass in JIT and interpreter
modes after the changes. CPUID SSE4 feature bits remain disabled.

A separate diagnostic runtime temporarily advertising SSE4 gets Claude past
PCMPISTRI. Bun's crash-reporting path uses PMOVZXDQ, then ROUNDSD while printing
RSS. Widening support gets past the former; the latter remains unsupported.
The original reason Bun enters its crash reporter remains unknown. Diagnostic
logs: `/private/tmp/claude-pmov-probe.*`. Temporary CPUID changes were restored
before rebuilding the default executable; `blink-pmov-diagnostic` is explicitly
not a supported feature configuration.

The code-server installer checkpoint is running in the prepared Debian rootfs
with fakeroot, Blink interpreter mode and TCP DNS. Default-TLS curl downloaded
`code-server_4.141.0_amd64.deb`; guest dpkg has reached unpacking. Logs:
`/private/tmp/blink-code-server-install.*`. Installation is not yet verified.

The UNPCKHPD-fixed normal-optimization TypeScript retry is now terminal: exit
137 after V8 reports an OptimizeGraph/Typer fatal error and Blink logs SIGTRAP
while stopping the process. The SIMD correction did not resolve this failure.

Blink `a2b148c` corrects floating-point comparison mask encoding: CMPPS/PD/SS/SD
were storing floating-point -1.0 for true instead of all-one bits. It also
corrects unordered handling for negated less-than/less-or-equal predicates.
All eight predicates pass in packed/scalar single/double forms (128 cases),
including NaNs, signed zero, upper-lane preservation and flags, in both modes.
The actual `npm run package` retry with normal optimization is running with
this fix: `/private/tmp/node-package-cmp-mask.*`. Its result is not yet known.

The code-server installer configured version 4.141.0 successfully, then the
Copilot-directory removal failed with `fts_read` ENOENT. A traced retry of
that removal exits 0, and the directory is absent. The guest dpkg database
confirms `install ok installed`, amd64, version 4.141.0. The first traversal
failure remains unexplained. A code-server --version smoke is running with
the comparison-mask fix; log: `/private/tmp/code-server-version.log`.

The code-server --version smoke now exits 0: version 4.141.0, commit
8b41915d25547e2edd553bab03ea4cbd621f98c5, with Code 1.141.0. Installing the
Claude Code extension and diagnostic command-center VSIX under fakeroot coder
identity is now running; logs: `/private/tmp/blink-code-server-extensions.*`.
The Dockerfile settings write and final runtime validation still remain.

The comparison-mask-fixed normal npm packaging retry is terminal: exit 137
after V8 reports unreachable code in BodyDescriptorApply / Scavenger::Process
on a garbage-collector worker. The mask fix is validated independently, but
normal V8 execution remains unresolved.

Blink `e7ab4d2` implements ROUNDPS/PD/SS/SD with integer IEEE rounding,
MXCSR mode selection, precision suppression, DAZ, NaN quieting, sticky status
and unmasked exception delivery. Its 363 value/status cases and separate
exception/guard-page tests pass in both modes. `351e452` adds PTEST with tests
for both 64-bit halves, flags, memory/register operands and aliasing.

Bun's crash reporter now completes in the temporary SSE4-advertising diagnostic.
Its generic AVX warning was not the actual instruction fault: the first address
was PTEST. After that fix, the next illegal instruction is BLENDVPS at
0x3c4c106. Logs: `/private/tmp/claude-ptest-probe.*`. Production CPUID still
leaves SSE4 feature bits clear. Full Claude startup remains unverified.

The single-threaded TypeScript diagnostic is running in
`/private/tmp/node-tsc-single-cmp-mask.*`. The code-server job has reached
`Installing extension 'anthropic.claude-code'...`; neither extension-install
completion nor the following settings write is yet verified.

Blink `6c29739` adds six SSE4.1 blend forms (48 cases); `db4ba41` adds eight
integer min/max operations; `7a7c310` adds PINSRB with all-lane and register-byte
selection checks. All fail before and pass after in both modes. The temporary
SSE4-advertising Claude probe now gets past these operations and terminates via
abort; successful startup is not yet established. Its logs are in
`/private/tmp/claude-pinsrb-probe.*`, with an additional syscall trace in
`/private/tmp/claude-pinsrb-strace.*`.

The single-threaded Node diagnostic completed TypeScript `tsc -p . --noEmit`
with normal optimization, exit 0. Disabling concurrent workers changes the
failure, but the exact underlying defect is still unknown. Code-server reports
Claude Code extension 2.1.310 successfully installed; installation of the local
command-center VSIX is still running. Settings and final image verification
remain outstanding.

Blink `db3da95` maps Linux coarse clocks to host coarse/fast clocks or the
corresponding standard clocks, with the actual host clock resolution. Epoch,
resolution and monotonicity checks fail before and pass after in both modes.
Bun still aborts with this fallback; the clock failure was not its cause.

The code-server extension job hit its 900-second limit after installing
Claude Code 2.1.310. The saved extensions.json independently confirms that
installation. A separate bounded retry runs only the local command-center
VSIX install and then writes the Dockerfile settings; logs:
`/private/tmp/blink-command-center-settings.*`. It uses the earlier diagnostic
VSIX. Final artifact ownership must also be rechecked because timeout can
interrupt fakeroot's state-save cleanup.

The command-center retry completed with exit 0. The extension registry now
contains anthropic.claude-code 2.1.310 and org.command-center 0.1.0, and the
three requested code-server settings are present. This uses the diagnostic
VSIX described above, not a successful normal-concurrency npm package build.
Logs: `/private/tmp/blink-command-center-settings.*`.

Claude's initialization abort was narrowed to a thread stack-bounds check.
`ext/blink/tool/thread_stack_probe.py`, executed with Debian's Python/glibc,
reproduces pthread_getattr_np returning ENOENT. A temporary guest-address
/proc/self/maps diagnostic changes this to success (stack base
0x4fffff804000, size 8368128) and moves Claude past the abort. That diagnostic
uses real contiguous guest ranges but placeholder permissions/backing fields;
it is not a production maps implementation and was restored out of the source.
The reproduction helper is `/private/tmp/blink-maps-diagnostic.py`; its binaries
named `blink-maps-diagnostic` and `blink-maps-fixed` BOTH contain the temporary
maps implementation. Only the former advertises SSE4. The normal binary and
source have neither diagnostic change.

Blink `3243a70` implements PEXTRD/Q, the next missing instruction after the
stack-query workaround. Twenty-four cases cover every lane, ignored upper
immediate bits, extended registers, dword zero extension, unaligned memory
writes with canaries, source preservation, and unchanged ZF. Both modes pass;
the previous interpreter traps. Claude's combined diagnostic now stops on
PACKUSDW at 0x5520307. Logs: `/private/tmp/claude-maps-probe.*`.
Production SSE4 feature bits remain disabled. Proper guest maps support,
normal concurrent V8 execution, and complete image export remain outstanding.

Blink `62261af` adds PACKUSDW (12 saturation, operand-order, aliasing, memory,
source-preservation and flag cases); `61f71c6` adds signed PCMPGTQ (30 cases,
including INT64 boundaries and low-dword distinctions); `144df06` adds
EXTRACTPS (17 cases, including ignored REX.W, zero extension, all lanes and
memory canaries). Each old interpreter traps; each patched test passes in
both interpreter and JIT modes.

With these fixes, the combined diagnostic **successfully executes Claude
2.1.310 --version, exit 0**, printing `2.1.310 (Claude Code)`. The successful
interpreter invocation is the latest run in `/private/tmp/claude-maps-probe.*`
(the .log file is append-only and includes earlier failures). A copy of the
successful diagnostic binary is `ext/blink/o/blink/blink-claude-version-diagnostic`.
It still includes temporary maps and SSE4 advertisement, plus the uncommitted
epoll/eventfd backend. This does not establish production CLI compatibility,
installer completion, or a complete image. The ordinary binary and source
have the maps/CPUID diagnostics removed.

The same diagnostic also passes Claude `--version` with JIT enabled (exit 0;
`/private/tmp/claude-jit-version.*`). Interpreter `--help` still traps on
PEXTRB at 0x545602f; logs `/private/tmp/claude-interp-help.*`. No authenticated
Claude request or external crash report was sent.

Blink `56a5128` adds SSE4 PEXTRB and PEXTRW forms. The test covers all byte
and word lanes, high immediate masking, zero extension, byte/word memory
canaries, EBP selection without REX, and raw encodings with ignored REX.W.
Both modes pass; the old interpreter traps. Claude --help advances to
PCMPEQQ at 0x1e13c6b. `f34489c` adds that operation with equality/inequality
in either 64-bit lane, high/low-dword distinctions, aliasing, register/memory
sources and preserved flags/source. Both patched modes pass; the old
interpreter traps. The subsequent bounded help retry uses logs
`/private/tmp/claude-pcmpeqq-help.*`.

A source audit against [Intel's SSE4 instruction reference](https://www.intel.com/content/dam/develop/external/us/en/documents/d9156103-705230.pdf)
identifies missing dispatch entries for PMULDQ (0x228), INSERTPS (0x321),
DPPS/DPPD (0x340/0x341), MPSADBW (0x342), and PHMINPOSUW (0x241).
This is a work list, not a claim that everything else is fully correct.
The existing CRC32 handler also needs validation of its operand-size-prefix
path and concurrent table initialization before advertising SSE4.2.
Accurate /proc/self/maps additionally needs retained shared/private mapping
metadata; current guest page flags do not distinguish this property.

The PCMPEQQ-fixed combined diagnostic completes Claude `--help` in interpreter
mode: exit 0, 313 lines of usage text, empty stderr. The same diagnostic in
JIT mode fails with guest SIGSEGV at 0x4fffff7ffff8; version-only success did
not establish this broader path. Logs: `/private/tmp/claude-pcmpeqq-help.*`
and `/private/tmp/claude-pcmpeqq-jit-help.*`. The immutable diagnostic binary
for these checks is `ext/blink/o/blink/blink-claude-help-diagnostic`.

A bounded 900-second interpreter retry of the actual official Claude install
command is running with fakeroot coder identity and TCP DNS. It uses that
immutable diagnostic binary, so maps, experimental feature advertisement,
and epoll/eventfd caveats still apply. Runner:
`/private/tmp/blink-claude-installer-retry.py`; logs and PID:
`/private/tmp/blink-claude-installer-sse4.*`. Installation success is not yet
established; do not infer completion from the successful help command.

Blink `68194cb` implements PMULDQ, INSERTPS and PHMINPOSUW, closing three
items from the feature-advertisement audit. PMULDQ tests signed INT32 boundary
products, ignored odd dwords, aliasing, memory/register inputs, source and
flag preservation. INSERTPS tests all 256 control bytes in register, memory
and alias forms (768 cases), plus a protected next page to verify the memory
operand reads only four bytes even when its source-selection bits are set.
PHMINPOSUW tests every possible minimum index, unsigned high-bit values,
first-index tie selection, upper-result clearing and aliasing. All three old
interpreter programs trap; all three patched programs pass with interpreter
and JIT. CPUID bits remain clear; MPSADBW, DPPS/DPPD and the CRC32 audit remain.

The official installer retry remains live as of this checkpoint. Its child
process was confirmed consuming approximately one CPU core; no stdout/stderr
completion marker has appeared. It uses the immutable older diagnostic binary,
so these new instruction changes do not alter the in-flight run.

Blink `7af2802` adds MPSADBW with 144 cases covering all block offsets,
ignored immediate bits, unsigned byte differences including the maximum
1020 sum, aliasing, memory/register sources and flags/source preservation.
The old interpreter traps; both patched modes pass. The operation follows
Intel SSE4 Programming Reference section MPSADBW (pages 80–82).

Blink `e480d2d` fixes CRC32's 16-bit source path, which previously rejected
the operand-size prefix, and replaces its unsynchronized table-init flag
with Blink's pthread_once wrapper. Twenty-four assembly cases cover all
four widths, multiple seeds, memory/register operands, zero extension and
flags/source preservation. A separate 16-thread guest test synchronizes
first use and compares 4096 results against a scalar polynomial reference.
Both tests pass in both modes. The old word-form probe traps; the table race
was identified by source inspection, not a reproduced incorrect checksum.

The official installer has downloaded the complete Claude 2.1.310 binary.
Native SHA256 of its download matches the previously verified binary:
`b73676430af12f97316a798b8371f81d640f90a58bdcb8f3ce56a835bb47df56`.
Its bounded process remains live; installation is not yet established.
SSE4.1 advertisement still awaits DPPS/DPPD and a final compatibility audit;
proper maps and the other build/runtime blockers also remain.

An uncommitted DPPS/DPPD implementation is now in `ext/blink/blink/dot.c`
with dispatch changes in machine.c/machine.h/name.c. It saves/restores the
host floating-point environment, applies guest rounding control, tracks
arithmetic flags, handles DAZ/FTZ, and defers destination writes until all
operations succeed. It is included in the current default development binary,
but production CPUID feature bits are still disabled. It is not yet in the PR.

Current validation passes in interpreter and JIT: `test/asm/dot_masks.S`
covers 1536 combinations (all 256 controls, both precisions, register/memory/
alias sources); `test/func/dot_fp_test.c` covers rounding modes, denormals,
DAZ/FTZ and overflow; `test/func/dot_exception_test.c` checks unmasked invalid,
denormal, overflow, underflow and precision exceptions and inspects the guest
signal context to verify the destination register is unchanged. Broader NaN
forwarding, exact exception priority and host/compiler portability still need
validation before committing this implementation or advertising SSE4 support.
Temporary compiled probes are `/private/tmp/dot_masks`, `dot_fp_test`, and
`dot_exception_test`; build log is `/private/tmp/blink-dot-build.log`.

The official installer retry remains live and now prints
`Setting up Claude Code...`. This is progress beyond the download/checksum
stage, not proof of installer completion. Its immutable diagnostic binary
predates the dot-product changes and is unaffected by development builds.

The official installer retry is terminal: exit 139 after its downloaded
binary runs `install`. Bun reports a worker-thread SIGSEGV at address zero.
The top-level command reached setup, but neither a launcher nor installed
version directory was created. This was a guest failure before the timeout,
not a killed or still-running job.

A direct retry of the same verified binary's `install` command reproduces
exit 139 in about a minute, avoiding the slow download/checksum stages.
Guest runner: rootfs `/tmp/install-claude-direct.py`; host runner:
`/private/tmp/blink-claude-install-direct.py`; logs:
`/private/tmp/blink-claude-install-direct.*`. The immutable runtime is
`ext/blink/o/blink/blink-install-segv-diagnostic`. It includes temporary maps,
SSE4 advertisement, signal tracing and the current uncommitted dot prototype.
Tracing changes were restored out of source/default binary after building.
Both installer jobs are now terminal; no installer is running.

The original fault occurs at guest IP 0x2652509, `mov (%r15),%r12`, with
R15=0. The preceding instruction loads R15 from RBX+0x48; RBX was
0x2c61d40e2f0 in the direct run. Captured frames: 0x2652509, 0x2653766,
0x265339a, 0x2671bda, 0x26666f9. This is in code near the exported
__posix_memalign symbol, suggesting allocator involvement, not yet a proven
allocator root cause. The signal trace records the original fault before
Bun's crash reporter changes the register state.

Dot-product validation found and corrected a real ARM/x86 difference:
0 * infinity produced a positive host NaN, but x86 requires negative indefinite.
The implementation now handles NaN priority/payloads and generated indefinite
explicitly. Updated floating-point, mask and exception tests pass in both
modes. Linux GCC 7 cross-compilation initially warned that STDC fenv pragmas
were ignored; GCC-specific rounding-math and fp-contract pragmas now compile
without those warnings. These are not native x86 execution results. Dot code
remains uncommitted pending the broader exception/portability review.

Allocator investigation matched the fault to mimalloc's page-free path:
Bun v1.4.4 pins oven-sh/mimalloc 92ef6587c57b262f6e6abf513f7776a2f8d12aeb,
now checked out cleanly in `ext/mimalloc`. The embedded Bun revision itself
was not available publicly, so this source match uses its release tag and
matching field layout/disassembly, not a verified embedded build manifest.
The null pointer is consistent with page->heap in mi_arenas_page_free_ex.

Blink `3b8efc6` corrects Linux MADV_DONTNEED for non-linear private anonymous
non-executable data: resident pages are zeroed while mappings/protections
remain intact; uncommitted reservations already read as zero. Unsupported
linear, file-backed, shared, and executable ranges return EINVAL instead of
claiming destructive advice succeeded. This is not complete madvise support;
other advice retains its existing behavior, and resident memory is eagerly
cleared rather than physically released. `tool/madvise_dontneed_probe.c`
(moved to explicit tooling by `7290a5e`, because the general functional
harness also requires linear-mode support) fails before with stale bytes and
passes in both -jm and -m modes. It checks partial-length page rounding,
neighbor preservation, PROT_NONE/PROT_READ preservation, invalid alignment,
unmapped ranges, and no accidental zeroing of shared memory.

The direct installer retry with this fix has passed the prior null-dereference
point and now prints `Installing Claude Code native build latest...`.
Runner: `/private/tmp/blink-claude-install-madvise.py`; logs/PID:
`/private/tmp/blink-claude-install-madvise.*`; immutable runtime:
`ext/blink/o/blink/blink-install-madvise-diagnostic`. It is still running with
a 900-second bound. This supports the stale-memory hypothesis but does not
establish complete installation or eliminate other allocator/emulation bugs.


Blink `80301c6` extends MADV_DONTNEED to non-linear private anonymous
executable pages. The traced normal Node package run rejected an anonymous
executable range (flags 0x207), triggering V8's DiscardUnusedMemory assertion.
The fix zeros those pages and invalidates both JIT translations and instruction
caches. `tool/madvise_exec_probe.c` failed at madvise before the fix and passes
with -jm and -m afterward, checking zeroed bytes and execution of replacement
code. The existing data-page discard probe still passes in both modes.
File-backed, shared, and linear mappings remain unsupported by this helper.
Normal package retry logs: `/private/tmp/node-package-madvise-exec.*`;
runner has an 1800-second bound. No successful normal package build is claimed.


Blink `3ddde4b` fixes a separately reproduced nested signal-wait hang.
DeliverSignalRecursively used SIG_BLOCK with an empty set when it intended to
unblock host signals during a handler entered from sigsuspend. A handler that
then called sigsuspend could never receive a later thread-directed wakeup.
The regression test times out before the change and passes afterward: 3/3
runs each with -jm/-m, plus default, -j, -sss, and -msss. A simple prequeued
signal control passes before and after. The original Claude installer is
waiting in a signal handler's sigsuspend (sample saved at
`/private/tmp/claude-install-madvise.sample.txt`); whether this correction fixes
that installer hang still requires a fresh diagnostic run.

The normal package retry with executable discard support exited 137, with
V8 reporting `Unknown type: 39178` and unreachable code in BodyDescriptorApply
from its concurrent scavenger. The discard assertion is absent from this run,
but the earlier concurrent-GC failure remains. Logs:
`/private/tmp/node-package-madvise-exec.{stdout,stderr,log}`.

The prior MADV_DONTNEED Claude installer attempt reached its 900-second timeout
while waiting in sigsuspend. A fresh attempt includes both `80301c6` and
`3ddde4b`, plus the explicitly diagnostic proc maps/SSE4/epoll runtime.
Runner/logs: `/private/tmp/blink-claude-install-sigsuspend.py` and
`/private/tmp/blink-claude-install-sigsuspend.*`; immutable binary
`ext/blink/o/blink/blink-install-sigsuspend-diagnostic`. This retry also has a
900-second bound and has not yet established installation success.


Blink `3316186` fixes MOVNTI reading XMM state instead of the specified GPR
source. Eight assembly cases fail before and pass with -jm/-m afterward,
covering 32/64-bit stores, RAX/R9, aligned/unaligned destinations, preserved
flags, and neighboring bytes. Intel SDM Vol. 2 MOVNTI defines the GPR source:
https://www.intel.com/content/dam/www/public/us/en/documents/manuals/64-ia-32-architectures-software-developer-vol-2b-manual.pdf
No causal link to the Node failure is established. A normal npm retry uses
`/private/tmp/node-package-movnti.py` and logs of the same prefix; its immutable
runtime logs the first actual MOVNTI execution per process. Source logging
was restored before committing; it is diagnostic only.

A small allocation/retained-value workload completes 655,360 objects with
normal and single-threaded GC under -jm; it does not reproduce the TypeScript
failure. Files: `/private/tmp/blink-node-gc-probe.{js,py}` and
`/private/tmp/node-gc-{normal,serial}.{stdout,stderr}`. The real compiler is
also running with only `--no-parallel-scavenge`, through
`/private/tmp/node-tsc-no-parallel-scavenge.py`, as a diagnostic comparison.
These flags are not proposed replacements for the Dockerfile commands.


Blink `39d7ce3` generalizes signal-handler host-mask restoration to handlers
entered from epoll_pwait as well as sigsuspend. The Claude retry still waited
in sigsuspend, but its sampled stack showed delivery from EpollPwait, where
all host signals remained blocked. `tool/epoll_signal_probe.c` reproduces
that path and times out before this fix; it passes afterward, 3/3 each with
-jm and -m. Both the nested-sigsuspend regression and simple pending-signal
control also pass 3/3 each in both modes. The handler temporarily gets an
empty host mask; its guest mask remains authoritative, and the original host
mask is restored after the handler returns. The epoll tool requires the
experimental backend and is not installed in the general functional suite.

The stalled installer process group 98362 was explicitly stopped after its
stack sample and unchanged CPU time confirmed the wait. Its runner exited
-9 (not an independent crash or timeout). A fresh bounded 900-second retry
uses `/private/tmp/blink-claude-install-epoll-signal.py` and matching logs;
immutable runtime `ext/blink/o/blink/blink-install-epoll-signal-diagnostic`.
This includes the diagnostic maps/SSE4/epoll prototypes. Complete installation
is still unverified.


Blink `13d5889` fixes REP MOVSB with identical source and destination. The
previous fast path skipped the instruction entirely, leaving RCX/RSI/RDI
unchanged and suppressing access faults. A one-byte case reproduced RCX=1
instead of zero; a PROT_NONE self-copy incorrectly returned without SIGSEGV.
The normal page-copy path now executes even when addresses coincide.
Validation in -jm and -m: 16 count/address-width/direction cases (including
zero count and two pages), retained-byte comparison, inaccessible nonzero
copy versus zero-count no-access, and existing forward/reverse copy tests.
No link to the full Node/Claude failures is established yet; live retries
use immutable binaries from before this correction.

The real TypeScript compiler completed successfully (exit 0, empty stderr)
with only `--no-parallel-scavenge`, using
`node --no-parallel-scavenge node_modules/typescript/bin/tsc -p . --noEmit`.
The runner's generic `normal npm package 0` label is misleading: this result
is the compiler control, not packaging. Logs use
`/private/tmp/node-tsc-no-parallel-scavenge.*`. This narrows the outstanding
Node investigation to parallel scavenging; it does not establish a runtime fix.


The real TypeScript compile control with only `--no-parallel-scavenge`
completed with exit 0 and empty stderr. Its runner prints the generic label
`normal npm package 0`, but the executed command was tsc with --noEmit, not
packaging. This is evidence for narrowing the concurrent-scavenger issue,
not proof that disabling it is a fix. The expanded retention/sharing probe is
`/private/tmp/blink-node-promotion-probe.{js,py}`; normal tracing shows multiple
scavenger identities. Normal and no-parallel runs are currently bounded at
600 seconds. Logs use `/private/tmp/node-promotion-{normal,serial}.*`.

A new installer sample at `/private/tmp/claude-install-epoll-signal.sample.txt`
shows the main thread actively executing guest code and polling epoll, rather
than remaining inside the old nested sigsuspend. Installation still has not
completed; the reserved versions/2.1.310 file is currently zero bytes.


Normal `npm run package` completed successfully with no NODE_OPTIONS or V8
workarounds in `/private/tmp/node-package-movnti.*` (exit 0). Both tsc and vsce
ran. The fresh eight-file archive is 9,382 bytes, SHA256
`48d4b7d6f6074c3d89ac1da5e4a4ab8610314e328003e28f9ce681adb9979a6d`.
ZIP integrity and extension package metadata (org.command-center 0.1.0) pass.
No MOVNTI execution was logged; this success does not establish that opcode
fix as the cause. A fresh normal retry with the newer runtime runs through
`/private/tmp/node-package-bit-atomic.py` (1800-second bound).
The successful archive is copied into Debian as `/tmp/command-center-normal.vsix`;
`/private/tmp/blink-command-center-normal.py` installs it as coder with --force
because an earlier diagnostic archive of the same version was already present.
Installation result remains pending.

The Claude installer progressed beyond the epoll/signal hang, then printed
`Download timed out: exceeded the total deadline`. Runner exited 137 with
`Killed` on stderr; this was not its external 900-second timeout. The reserved
version file remains zero length. No complete Claude installation is claimed.
Both expanded GC promotion controls completed with exit 0 and PASS 491520;
they still do not reproduce the full compiler's earlier failure.

Blink `e4f1bb1` fixes aligned LOCK BTS/BTR/BTC interoperability with other
locked operations. The prior bus mutex did not synchronize with locked ALU
host atomics: mixed bit changes and locked increments lost updates (e.g.
expected 20000, actual 5012/6310). Aligned 16/32/64-bit bit operations now use
host atomic fetch operations. The existing unaligned/software-bus fallback
remains; this is not a complete audit of overlapping or unaligned atomics.
Validation: six mixed-operation cases and 36 carry/result/neighbor cases,
3/3 runs each with -jm and -m. The first normal package success predates this
fix, so it is not proof of the earlier V8 corruption's cause.
An initial test that repeatedly recreated threads encountered a separate
stack-access fault; preserved executable `/private/tmp/bit_atomic_thread_reuse_probe`.
The committed atomic test keeps two workers alive across all cases to isolate
atomicity; the thread-lifecycle failure remains to investigate.


Blink `e22f3a1` adds `tool/thread_reuse_probe.c`: 256 guest thread creations
with stack writes and joins. It passes under -jm and faults under -m in both
the pre-atomic diagnostic runtime and current runtime (sample failure at
guest IP 0x40039e). This is an unresolved JIT lifecycle issue, not a fix or a
normal test-suite addition. The earlier expanded atomic test also had one
interpreter-mode lifecycle fault; this smaller probe does not reproduce that
interpreter failure yet. Target Dockerfile probes remain in -jm mode.

The verified Claude binary's embedded installer code defines default total
download deadline 600000 ms and accepts the testing environment variable
`CLAUDE_CODE_DOWNLOAD_DEADLINE_MS_FOR_TESTING`. Prepared diagnostic runner
`/private/tmp/blink-claude-install-deadline.py` sets it to 1800000 ms and uses
an external 2400-second bound. Its immutable runtime is
`ext/blink/o/blink/blink-install-deadline-diagnostic`. It has NOT started yet:
waiting for the normal VSIX installer to finish before another writer uses
the same Debian root/fakeroot state. A pass with this override would establish
a diagnostic checkpoint, not unchanged-command performance.


Blink `ca79a70` fixes the shared host-page table growth race. The failing
thread probe resolved a valid stack page to NULL at host-page index 205 just
as the table grew from 208 to 313 entries. Unsynchronized realloc could free
storage still used by readers; concurrent appenders also lacked serialization.
Writers now hold the allocator lock and atomically publish replacement tables.
Older tables remain reachable for lock-free readers, including signal handlers.
This retains previous table allocations for process lifetime (additional table
memory); entries are copied during growth. Fork now takes the allocator lock
so a child inherits consistent allocator/table state. Linear mappings retain
their direct-address path.

Validation: thread-reuse and original repeated-thread atomic probes each
passed 10/10 under -jm and -m. With fork synchronization, thread reuse and a
concurrent-allocation/20-fork probe each passed another 3/3 per mode. The
thread diagnostic was promoted from tool/thread_reuse_probe.c into
`test/func/thread_reuse_test.c`; fork coverage is in
`test/func/thread_fork_pages_test.c`. Local generated dependency files were
missing, so all o/blink objects were rebuilt after the shared header changed;
the first stale-object build was rejected, not counted as validation.

The second normal npm package retry (`/private/tmp/node-package-bit-atomic.*`)
also exited 0 and produced the VSIX. Both normal successes predate ca79a70.
The fresh-VSIX reinstall hit its external 300-second bound and left only a
`.vsctmp` extension directory, so the code-server checkpoint currently needs
that interrupted installation completed. Old and new VSIX payloads are byte
identical. No successful fresh reinstall is claimed.

The longer-deadline Claude run completed the binary download: installed
versions/2.1.310 is 291638640 bytes and SHA256
b73676430af12f97316a798b8371f81d640f90a58bdcb8f3ce56a835bb47df56, matching the
verified release. It then failed removing its staging directory with EPERM;
~/.local/bin/claude is still absent. The run exited 137, not external timeout.

Blink `3783bda` fixes the cleanup error translation: SysUnlinkat's existing
EPERM-to-EISDIR check called host fstatat on a guest path/fd. It now uses
VfsStat with AT_SYMLINK_NOFOLLOW. The regression failed before and passes in
-jm/-m for absolute and dirfd-relative directories, symlink removal, regular
files, AT_REMOVEDIR and ENOENT. Linux unlink behavior reference:
https://man7.org/linux/man-pages/man2/unlink.2.html
A fresh `/private/tmp/blink-claude-install-unlink.py` run uses normal installer
limits with the verified version already cached; external bound 900 seconds.
Its immutable runtime is `ext/blink/o/blink/blink-install-unlink-diagnostic`
(still includes diagnostic proc maps/SSE4 and experimental epoll support).
A cached success would not prove unchanged-command fresh-download performance.


The VFS-enabled build cannot run its bootstrap dependency scanner when it tries
creating `/SystemRoot` on the macOS host. Use the existing non-VFS Blink for
bootstrap tools: `make -j4 'VM=o/opt/blink/blink -j' o//blink/blink` from
`ext/blink`. This `VM` Makefile variable selects userspace emulation, not a
hardware VM. Dependency generation now succeeds; header changes rebuild their
consumers instead of leaving stale objects.


Repeatable Debian checkpoints use `scripts/blink-checkpoint.py`. It requires
Python 3 and fakeroot-tcp inside the guest, preserves an immutable Blink copy
and JSON result beside stdout/stderr and the Blink log, and refuses concurrent
runs against the same rootfs. Older temporary scripts do not use its lock; wait
for them to finish before starting another writer. A new output directory is
required for every invocation. Example for the native installer checkpoint:

```sh
python3 scripts/blink-checkpoint.py \
  --blink ext/blink/o/blink/blink-maps-diagnostic \
  --rootfs target/blink-experiment/run-Bb8jmS/rootfs \
  --output target/blink-experiment/claude-native-install-checkpoint \
  --uid 1000 --gid 1000 --home /home/coder \
  --workdir /home/coder/workspace --dns-tcp --timeout 900 \
  --env USER=coder --env LOGNAME=coder \
  -- /home/coder/.local/bin/claude install
```

The diagnostic Blink binary in this example still uses the temporary maps and
SSE4 implementations described above. The runner uses fakeroot for ownership
bookkeeping; it does not implement Linux permission isolation or Dockerfile USER
semantics by itself.


Checkpoint-runner validation is retained under
`target/blink-experiment/checkpoint-runner-identity` and
`target/blink-experiment/checkpoint-runner-timeout`. The identity test printed
UID/GID 1000, the requested workspace, HOME and environment value, then preserved
intentional exit status 7. The bounded sleep test returned 124, recording the
killed process status and `timed_out: true`. The initial identity test exposed
that passing Python's cached environment through exec loses fakeroot's libc
UID/GID updates; the runner now inherits libc's environment through `execvp`.


The normal command-center VSIX clean install completed with exit 0 in
`/private/tmp/blink-command-center-clean-install.*`. Content checks for the 13
org configuration/agent/skill files, the coder account, editor settings, and
Copilot removal are recorded in
`target/blink-experiment/agent-platform-files-audit.json`. These checks do not
prove filesystem ownership or runtime permission enforcement.

The exact Claude-base command `curl -fsSL https://claude.ai/install.sh | bash`
completed as coder with exit 0 in 841.8 seconds through the reusable checkpoint
runner, including a new release-binary download. Its evidence
is under `target/blink-experiment/claude-dockerfile-bootstrap`; `result.json`
records completion only when `finished` and `exit_code` appear. This rootfs
already had the native installation, so this remains a checkpoint rerun,
not a fresh image build.


Filesystem changesets can now be generated with `scripts/oci-diff.py`:

```sh
python3 scripts/oci-diff.py --before before.tar --after after.tar --output change.tar
cargo run --example oci-assemble -- --base base-layout --layer change.tar --output derived-layout
python3 scripts/test-oci-diff.py
```

Inputs are filesystem snapshots, including parent directory entries, with the
**guest** numeric ownership and modes already recorded. Capture them under the
guest's fakeroot session, not by archiving host-owned extracted files. The tool
compares contents and metadata without extracting archives, generates explicit
OCI whiteouts, preserves symlinks and hardlink groups, and writes changed entries
as a PAX tar. Sparse archives and malformed/ambiguous snapshots are rejected.
This is a changeset primitive; automatic full-stage capture and exclusion of
runtime mounts/control artifacts remain builder work.

The integration fixture under `target/blink-experiment/diff-integration` feeds a
computed changeset into the assembler. Its derived image passed descriptor and
DiffID validation, removed the deleted file, preserved UID/GID 1000 and mode 0640
in the new layer, and ran its configured command through Blink with exit 0.
The fixture applier handles OCI whiteouts explicitly; plain tar extraction is
not a general OCI layer applier.

Actual guest snapshot capture initially failed with GNU tar's `Cannot savedir:
Not a directory`. Blink `45ce00e` replaces getdents' O_DIRECTORY flag requirement
with a check of the descriptor's actual file type, matching Linux
[getdents semantics](https://man7.org/linux/man-pages/man2/getdents.2.html).
The regression failed in both modes before the fix and passes 3/3 per mode after.
The rerun at `target/blink-experiment/guest-diff-capture-fixed` exits 0; its
`delta.tar` contains `.wh.removed`, root directory metadata, and `message` with
UID/GID 1000, mode 0640, and the contents written inside Blink.


Final-entrypoint testing is now tracked separately from installation. Running
`dumb-init code-server` on localhost initially spun on missing syscall 0x80
(rt_sigtimedwait), producing about 314 MB of repetitive logs in 30 seconds.
That confirmed spin was stopped; evidence remains at
`target/blink-experiment/code-server-runtime`.

An **uncommitted signal prototype** adds bounded sigtimedwait polling and pending
siginfo records. The probe `ext/blink/tool/sigwait_probe.c` now passes in both
modes for pending and delayed delivery, zero/finite timeout, sender PID/UID,
SIGCHLD exit status, EINTR, mask restoration, and invalid arguments. Existing
nested-signal, epoll-handler, and thread-shutdown regressions still pass. The
cross toolchain's musl sigwaitinfo wrapper retries EINTR (confirmed by its local
disassembly), so the EINTR assertion uses the syscall directly. Real-time signal
queuing and process-wide signal delivery/default-disposition handling still need
review; this prototype is not a complete Linux signal implementation.

With the prototype, the interpreter gets past dumb-init but code-server reports
its own startup timeout (`out/node/wrapper.js` uses a 10000 ms handshake timer).
Its orphaned worker was sampled in guest futex/epoll waits, then stopped so the
fakeroot session could finish saving; the runner exited 1. Evidence is under
`target/blink-experiment/code-server-runtime-sigwait` and
`/private/tmp/code-server-startup-orphan.sample.txt`.
The JIT comparison under `target/blink-experiment/code-server-runtime-jit`
exited 132 with SIGILL on a Node worker (guest RIP 0x27b8700, displayed instruction
`push %rbp`, host fault address 0x1009b17d0). Neither run reached HTTP readiness.
The signal changes remain uncommitted pending further isolation of these runtime
failures.


The checkpoint runner now cleans up detached workers by matching its unique
immutable Blink executable path, with a second identity check before signaling.
Process-group cleanup alone missed descendants that called setsid. Two escaped
workers from the initial runtime test were stopped after backing up and verifying
the latest fakeroot database. The controlled detached-child test under
`target/blink-experiment/checkpoint-runner-detached` returned 124 at its five-second
bound, recorded the escaped worker PID, and left no matching process running.
This cleanup is scoped to each run's unique executable path, not all Blink
processes. It does not turn Blink into a security sandbox.

### Node runtime regressions (2026-10-24)

The guest code-server distribution supplies Node 24.21.0. The following small
probes isolate runtime behavior from the full application:

- `examples/node-ipc-smoke.js`: fork, piped output, two-way IPC, child exit;
  passes under Blink JIT (checkpoint `node-ipc-jit-pool-fixed`).
- `examples/node-package-resolution-smoke.js`: repeated package resolution and
  import; before `21e5c50`, interpreter passes while JIT reports
  `ERR_INVALID_PACKAGE_TARGET` for the valid `./index.js` target.
- `examples/node-url-smoke.js`: reduces that failure to URL resolution;
  before `21e5c50`, interpreter passes while JIT returns an empty directory
  pathname on iteration 19.

Evidence is under `target/blink-experiment/node-{package-resolution,url}-*`.
The code-server attempt with fixed JIT pool reached its handshake timeout and
was stopped at the runner's 180-second bound; escaped workers were cleaned up.
The diagnostic 120-second handshake grace attempt failed earlier in Node package
resolution, so it provides no evidence of HTTP readiness. No application source
was changed: the grace attempt used a temporary Node preload hook.

The URL and Uint32 failures described above are fixed by Blink commit `21e5c50`.
`node-movsd-fixed` records the passing URL and 10,000-iteration Uint32 probes,
then successful package resolution followed by SIGSEGV during package import
(guest RIP `0x122d81e`, fault address `0xaa`). The combined checkpoint exits 139;
it is not a complete passing package-import test. The native regression
`test/func/movsd_page_store_test.c` fails with exit 2 before the fix and passes
under both JIT and interpreter afterward. Temporary address-range exclusions
were restored in source; they are diagnostic controls, not the fix.

### Verified diagnostic HTTP startup

`target/blink-experiment/code-server-http-check/http-results.json` records HTTP
200 responses from `/healthz` and `/`, with the latter resolving to
`/?folder=/home/coder/workspace`. Response bodies are saved beside it. The health
body reports `expired` with `lastHeartbeat: 0` because this probe does not open an
editor session; the evidence establishes HTTP serving, not a live browser session.
The server uses an in-memory preload override of the handshake deadline (120s),
the native-fault diagnostic Blink build, and the existing signal/epoll prototypes.
The runner ended at its intentional 240-second bound and cleaned up remaining
workers. `code-server-movsd-fixed` retains the stock-deadline failure evidence.

The full fresh Dockerfile builds, Dockerfile-driven stage capture, and final image
export remain outstanding. HTTP success in the manually assembled rootfs does
not prove those builds are complete.

### Capturing a checkpoint as a filesystem layer

`blink-checkpoint.py --snapshot-root /` now runs guest GNU tar before and after
the command in the same fakeroot session, then generates `layer.tar` with
`oci-diff.py`. Snapshots run as virtual root; the command uses the requested
UID/GID. An output directory outside the rootfs is required. For example, on a
prepared Debian rootfs containing fakeroot-tcp and Python:

```sh
python3 scripts/blink-checkpoint.py \
  --blink ext/blink/o/blink/blink-native-fault-diagnostic \
  --rootfs target/blink-experiment/run-Bb8jmS/rootfs \
  --output target/blink-experiment/hello-layer-checkpoint \
  --snapshot-root / --timeout 900 \
  -- /bin/sh -ec 'printf "Hello, world!\n" > /hello.txt'
```

Both archives retain numeric guest ownership. The active runner directory,
fakeroot database, `/SystemRoot`, and contents of `/proc`, `/sys`, and `/dev` are
excluded; tar does not follow symlinks or cross filesystems. Existing diagnostic
files elsewhere in the rootfs are not automatically removed. This is not an
extended-attribute/capability-preserving snapshot implementation.

Only successful commands with successful worker cleanup can publish a layer.
`result.json` includes `capture_complete` and `layer_sha256`; a process exit code
alone does not establish successful capture. The timeout covers both snapshots
and command execution. A failed command may have changed the rootfs; this tool
does not roll it back. Other writers must honor the same rootfs lock.

Use a subdirectory with `--snapshot-root /some/directory` for small probes. Its
archive paths are relative to that directory, so such a layer must not be applied
as a full-rootfs changeset without explicitly relocating those paths. A full
rootfs layer belongs on the OCI image corresponding to its **before** snapshot;
the manually assembled experimental rootfs is not the pristine Debian base.

```sh
python3 scripts/test-blink-capture.py \
  --blink ext/blink/o/blink/blink-native-fault-diagnostic \
  --rootfs target/blink-experiment/run-Bb8jmS/rootfs \
  --output target/blink-experiment/capture-validation
```

The integration run at `target/blink-experiment/capture-runner-integration-host`
passes guest identity, UID/GID 1000, mode 0640, hardlinks, symlinks, deletion
whiteouts, unchanged-file omission, and failed-command rejection. It requires
local socket access for fakeroot and host process inspection for worker cleanup.
Full-rootfs capture and assembly of the target images have not yet been validated.

### OCI comparison follow-up

The comparison reports in `target/oci-comparison/` found case collisions in
Perl `sys`/`Sys` and `pod`/`Pod` paths, plus inherited `Cmd: ["bash"]` in the
older code-server export. Target builds now reject case-insensitive output
filesystems. `ENTRYPOINT` explicitly clears an inherited command in the OCI
config overlay while preserving a command declared in the current stage.
Four builder tests and four OCI assembler tests pass, including explicit null
command override through assembly.

A case-sensitive APFS sparse bundle at `target/image-forge-build.sparsebundle`
is mounted at `target/case-sensitive-builds`. This is native filesystem storage,
not a Linux kernel or VM. A fresh Claude-base build has started under
`target/case-sensitive-builds/claude-base`; completion remains unverified.
The existing rootless-v3 artifact passed the narrower target verifier but has
the case-collision defect, so that verifier alone does not establish correctness.
Do not copy an already unpacked case-collided rootfs to this volume: rebuild
from the OCI base archives. The comparison used the older checkout. The subsequent case-sensitive builds
recorded updated source commit `084da8c` (see
`target/case-sensitive-builds/source-revision.json`), including GitHub CLI,
credential configuration and the newer code-server Dockerfile. Command Center
is being rebuilt from that source as well. The strengthened target verifier
rejects the old case-collided export and passes published code-server:24.


The isolated rootless metadata patch is committed as Blink `2ab186c`. All five
staged C/header sources matched the independently compiled clean checkout, and
all 14 interpreter/JIT probes passed using the regression tools now included
under `ext/blink/tool`. The backend remains opt-in metadata bookkeeping for
trusted build commands, with limitations documented in `tool/rootless-metadata.txt`.

### Case-sensitive Claude-base export completed

`target/case-sensitive-builds/claude-base/image` completed all Dockerfile
checkpoints, including the updated GitHub CLI installation/configuration and
Claude installer. Manifest:
`sha256:8c0186b4c72d3f32cfbe7dabf39140419eae2fc669531615604846c021185b6b`.
The strengthened archive-only verifier passes. The full-byte OCI comparison at
`target/oci-comparison/case-sensitive-claude-base.json` finds no ownership,
permission, symlink-target or path-case differences on shared entries versus
published Claude-base:5. Only five shared files differ: Claude generated state,
ldconfig cache, and three package-install logs. Local `/etc/hosts` and a published
Claude backup account for the remaining presence differences. Empty versus absent
Entrypoint remains a serialization difference. This is not byte-identical OCI.

Command Center's fresh `npm ci` JIT checkpoint terminated with SIGSEGV (exit139);
its result confirms worker cleanup. Evidence is preserved in
`target/case-sensitive-builds/command-center/failed-005-jit`. Reported guest RIP
`0x1ca3ec0` lies within Node's tree-erasure code, but falls inside an instruction
operand in static disassembly; this alone does not identify the faulty operation.
The unchanged command is being retried with Blink's interpreter. The final
code-server build awaits this updated stage; no completed editor is executed.

Command Center's unchanged `npm ci --no-audit --no-fund --ignore-scripts`
completed successfully under Blink interpreter mode after the preserved JIT
failure. Its successful receipt is `command-center/005-run/result.json` on the
case-sensitive build volume. `npm run package` is now running in interpreter
mode. `target/case-sensitive-builds/reproduce.sh` records the sequential target
commands, and `toolchain-hashes.json` records the current builder/runtime files.

For subsequent fault diagnosis, `build-targets.py --fault-trace` now forwards
bounded `BLINK_TRACE_FAULTS` logging to checkpoints. It can be enabled alongside
an interpreter/JIT change while preserving completed receipts; a regression test
checks that the successful command is not executed twice. Five builder tests
pass. The final code-server command in `reproduce.sh` enables this logging.

The interpreter TypeScript compilation completed: all five generated JavaScript
files match the published code-server:24 Command Center VSIX byte-for-byte.
Hashes are recorded in `target/oci-comparison/command-center-compiled.json`.
The subsequent VSIX packaging step is still running. The original 44-file build
context is preserved as `target/case-sensitive-builds/source-context.tar`, with
all content hashes checked against the stage's input record.

### Final code-server build started

The fresh Command Center stage completed and exported its OCI image at
`target/case-sensitive-builds/command-center/image`. Both `npm ci` and
`npm run package` passed in interpreter mode after the preserved npm JIT crash.
All nine files in its VSIX exactly match the published code-server:24 package;
ZIP archive hashes differ, but there are no member-content differences.
Evidence: `target/oci-comparison/command-center-vsix.json`.

The final code-server build is running at
`target/case-sensitive-builds/code-server`, using the fresh Claude-base OCI and
this completed Command Center stage, rootless metadata, and bounded fault logs.
Its final export and archive comparison remain pending.

### Final build audit

Both requested Dockerfile builds and OCI exports are complete. The audit at
`target/case-sensitive-builds/completion-audit.json` verified 26 successful
checkpoint receipts, both target verifiers, full filesystem metadata comparisons,
code-server runtime configuration equality, and all nine VSIX member contents.
`runtime-sources.tar` contains all 236 source files matching the immutable runtime's
recorded hashes, including the temporary diagnostic cpuid/procfs sources.

Exact-runtime compatibility probes are in
`target/case-sensitive-builds/runtime-validation/`. Epoll signal handling,
default-signal waiting and repeated eventfd edges pass in both modes. Inherited
eventfd writes fail with EBADF (exit 5), confirming the known shim limitation.
With rootless metadata enabled, sigwait's sender UID remains the native host UID
(exit 13); the same probe passes in both modes without rootless metadata. This is
an evidenced limitation of the experimental signal/virtual-credential integration,
not a claim that full Linux signal semantics are implemented. Neither limitation
prevented the requested builds. Finished editor/browser execution is outside the
user's clarified completion scope.
