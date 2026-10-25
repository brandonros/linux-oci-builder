# Runtime setup

The pinned Blink fork includes committed compatibility fixes and rootless metadata
support. `patches/blink.patch` preserves the additional experimental changes from
the original workspace, including signal/epoll work, fault diagnostics and SSE dot
product support. These are development patches, not claims of complete Linux semantics.
The epoll-shim submodule points to `brandonros/epoll-shim` commit `99adf8d`,
which includes the repeated eventfd notification fix and regression test in
[upstream PR #64](https://github.com/jiixyj/epoll-shim/pull/64).
The agent-platform submodule pins the Dockerfiles used for integration builds.

## macOS ARM64

After initializing submodules and running `scripts/apply-runtime-patches.py`, build
the shim and configure Blink from the repository root:

```sh
cmake -S ext/epoll-shim -B target/epoll-shim-build \
  -DCMAKE_INSTALL_PREFIX="$PWD/target/epoll-shim-install" -DBUILD_TESTING=OFF
cmake --build target/epoll-shim-build -j4
cmake --install target/epoll-shim-build

shim_prefix="$PWD/target/epoll-shim-install"
(cd ext/blink && \
  CPPFLAGS="-D_FILE_OFFSET_BITS=64 -D_DARWIN_C_SOURCE -D_DEFAULT_SOURCE -D_BSD_SOURCE -D_GNU_SOURCE -DBLINK_USE_EPOLL_SHIM -DEPOLL_SHIM_DISABLE_WRAPPER_MACROS -I$shim_prefix/include/libepoll-shim" \
  LDFLAGS="-L$shim_prefix/lib -Wl,-rpath,$shim_prefix/lib" \
  LDLIBS="-lepoll-shim" ./configure --enable-vfs && \
  gmake -j4 o//blink/blink)
```

The double slash in the Make target is intentional. If GNU Make is installed under
another name, substitute its path for `gmake`.

The larger integration builds also used temporary SSE4 advertisement and placeholder
procfs maps. To reproduce that diagnostic configuration after configuring Blink:

```sh
python3 scripts/build-blink-diagnostic.py --make /absolute/path/to/gmake \
  --output target/blink-runtimes/diagnostic
```

The script restores the modified source files and records runtime source hashes.
Use this runtime path with `build-targets.py --blink`, plus `--rootless-metadata`.
Use `--no-jit` for interpreter execution. The builder's `--help` lists inputs and
options for stage selection and `COPY --from` roots. See the historical build notes
for the target-specific checkpoints; those notes reference local artifacts that
are not distributed with the repository.

## Limits

- Build output must live on a case-sensitive filesystem, including on macOS.
- Virtual ownership and modes preserve image metadata; they do not isolate builds.
- Signal/epoll support is experimental. Inherited eventfd writes across fork and
  signal sender UID reporting with virtual credentials have known failures.
- Diagnostic procfs maps contain placeholder fields. SSE4 advertisement does not
  establish complete instruction support.
- Successful target builds do not establish general Dockerfile or Linux compatibility.
- The reusable Rust runtime described in `blink-rs-plan.md` is a plan, not an
  implemented replacement for the current Blink subprocess backend.

Some internal `image-forge` identifiers are retained for compatibility with existing
checkpoint paths and test fixtures. The package and repository are named
`linux-oci-builder`.
