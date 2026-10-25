# Plan: a reusable Blink runtime in Rust

Investigation date: 2026-10-25. This is a design and implementation plan, not an
implemented API. No runtime or build behavior was changed for this investigation.

## Recommendation

Pursue a Rust-owned runtime API and an incremental semantic port of Blink, keeping
the working C executable as a reference and fallback until workload parity passes.
Do not first spend the whole project turning the C implementation into a fully
reentrant library if the intended destination is Rust. A narrow C adapter can be
useful for experiments and differential tests, but is not a required first phase.

A full Rust port gives us a clean place to express ownership and lifecycle. It is
not established to be less work than C embedding. Both require resolving guest
process, signal, memory and shutdown semantics; the port additionally replaces the
CPU implementation, loader, syscall layer and eventually JIT. The C/header files
directly under `ext/blink/blink` currently total 67,786 lines, including comments,
headers, debugger and platform support, excluding third-party code and tests.

Keep two completion targets distinct:

1. **Builder parity:** AMD64 Linux guests on macOS ARM64, including the three
   stages used by our two agent-platform Dockerfiles.
2. **Whole-project parity:** the rest of Blink's host matrix, execution modes,
   debugger/terminal UI, optional configurations and supported binary behaviors.

The first is a useful release gate; it must not be described as the entire port.
ARM64 Linux guests are a subsequent feature, independently gated from either port.

## Source findings

Paths below are relative to the repository root. The checkout is based on Blink
commit `2ab186c3cb27fb35e5aab9b074716ed21e1638ec` and has additional modified and
untracked sources. A commit SHA alone does not describe the working build runtime.

| Area | Evidence | Consequence |
| --- | --- | --- |
| Linking | `ext/blink/blink/blink.mk` already builds `blink.a`, excluding the two frontend objects | Creating a static archive is not the missing capability; lifecycle is. |
| Entry | `blink/blink.c:main` initializes maps, VFS, signals and bus; `Exec` creates System/Machine and enters `Blink` | Separate frontend configuration from engine initialization and guest launch. |
| Guest exit | `blink/syscall.c:SysExitGroup` calls `exit` or `_Exit`; `SysExit` can call `pthread_exit` | Guest termination needs an explicit outcome, not termination of the embedding application. |
| Existing exit hook | `System.trapexit`, `kMachineExitTrap` and the blinkenlights handler | Useful precedent for a C experiment, not a complete reusable runtime. The normal `Blink` loop does not return an exit outcome. |
| Fatal signals | `blink/blink.c:TerminateSignal` kills the host process; `HandleSigs` installs process-wide handlers | A library cannot silently take ownership of the application's signal dispositions. |
| Nonlocal control flow | `blink/machine.c:Blink` and host signal handling use `sigsetjmp`/`siglongjmp` | Any retained C recovery boundary must remain wholly inside C; never jump over Rust frames with live values. |
| Processes | `blink/syscall.c:Fork` invokes host `fork`; guest threads use pthreads | This cannot simply be transplanted into a multithreaded Rust/Tokio caller. |
| Filesystem | `blink/vfs.c` has `g_vfs`, `g_rootinfo`, `g_cwdinfo`, `g_actualrootinfo` | Multiple stage contexts require explicit VFS ownership and process-local cwd. |
| Memory/lifecycle | `blink/memorymalloc.c` provides New/Free System and Machine; FreeSystem has a signal-lock destruction TODO | Existing constructors are useful references, not proof of complete repeatable teardown. |
| JIT | `blink/jit.c` has a global pool; `blink/uop.c` uses a micro-op emission DSL and special compilation flags | A Rust JIT needs deliberate lowering and code ownership; do not assume compiled Rust function bytes can replace C micro-op machinery. |
| Build integration | `scripts/build-targets.py` calls `blink-checkpoint.py`; that launches Blink once per checkpoint | Preserve checkpoint receipts, timeout/cleanup behavior and image metadata while replacing the backend. |
| Experimental dependencies | Local edits and `scripts/build-blink-diagnostic.py` add runtime behavior beyond the PR commit | Inventory signal/epoll adapters, epoll-shim patch, maps diagnostics and feature flags explicitly before comparing runtimes. |

## Proposed public API

Keep the shape small, with configuration and process state separated. Example API
syntax below is a proposal, not code that currently compiles:

```rust
let mut blink = Blink::create(RuntimeConfig {
    rootfs: stage_rootfs,
    guest_arch: GuestArch::X86_64,
    execution: ExecutionMode::Interpreter,
    metadata: MetadataMode::RootlessBuild,
})?;

let outcome = blink.run(
    Command::new("/bin/sh")
        .args(["-c", "npm ci --no-audit --no-fund --ignore-scripts"])
        .cwd("/src")
        .uid(0)
        .gid(0)
        .envs(build_env)
        .stdout(stdout_file)
        .stderr(stderr_file)
        .timeout(step_timeout),
)?;
outcome.ensure_success()?;

blink.close()?; // Explicit, reportable cleanup; Drop is a fallback.
```

Contract:

- `create` validates the root, host capabilities and requested engine mode. It
  does not launch a guest or mutate the host cwd/environment.
- One `run(&mut self, ...)` is active per context. Initial API makes no promise
  of Send/Sync or concurrent engines inside one host process.
- Every run starts a fresh guest process tree. Filesystem changes and persistent
  guest inode metadata survive. Registers, address spaces, credentials, cwd,
  signal state and descriptors start from that run's command configuration.
- Guest `execve` replaces a process image inside the run; it does not create a
  new builder-level run. Guest children are tracked until the run is cleaned up.
- `run` returns `RunOutcome` with exited/signaled/timed-out/cancelled status and
  cleanup evidence. Loader failures and engine failures are distinct errors.
  Nonzero guest exit is a valid outcome, not automatically an engine error.
- Stream output to files or bounded sinks, never buffer arbitrary build logs in
  memory. Initial sinks should avoid reentrant callbacks into the runtime.
- A timeout or completed top-level guest triggers descendant cleanup, following
  the current builder contract. A successful return requires verified cleanup.
- Cleanup failure poisons the context: reject another run and report the error.
  Dropping a handle alone is not evidence that escaped workers have terminated.
- Rootless metadata reproduces build output ownership/modes. It is not a Linux
  security model or a sandbox.

For a fresh current build: three stage contexts, 26 checkpoint runs, three closes.
That number describes the present driver, not an API invariant. Later host-side
COPY/snapshot implementations could reduce it, but are not part of this migration.

## Internal ownership

Start with a `blink-rs` library crate and a separate CLI/worker binary. Use modules
before splitting into many crates:

```text
api             RuntimeConfig, Command, RunOutcome, cancellation
runtime         lifecycle, process table, resource accounting
guest           address spaces, threads, descriptors, credentials, signals
arch/x86_64     decoding, registers, instruction semantics, Linux ABI decoding
linux           syscall semantics and guest kernel objects
vfs             mount namespace, hostfs, procfs, rootless inode metadata
host/darwin     host file/socket/poll/memory operations
engine          interpreter; later JIT
```

Use explicit guest addresses rather than assuming guest addresses are valid host
pointers. Use explicit Linux layouts and constants rather than Rust host `libc`
layouts for guest structures. Validate guest memory ranges and overflow centrally.
Keep host unsafe operations in small reviewed modules. Retain upstream copyright
and license notices in translated source and audit third-party components.

## The process-model decision must come early

### Decoder candidates: yaxpeax

The user proposed [yaxpeax-x86](https://github.com/iximeow/yaxpeax-x86) and
[yaxpeax-arm](https://github.com/iximeow/yaxpeax-arm). Their READMEs were inspected
on 2026-10-25. Use these as candidates for decoding instead of assuming we must
port Blink's decoder. This is not yet a dependency selection or coverage claim.

- `yaxpeax-x86::long_mode::InstDecoder` targets AMD64 decoding.
- `yaxpeax_arm::armv8::InstDecoder` targets AArch64/A64 decoding.
- Both participate in the `yaxpeax-arch` decoder interfaces. Shared decoding
  interfaces do not supply shared execution semantics.
- The ARM README lists outstanding coverage/validation work, including SVE/SVE2;
  its ARMv7 NEON warning should not be confused with AArch64 coverage.

Put each behind an architecture-specific adapter that returns typed operands,
instruction length, prefixes/modifiers and opcode information to our interpreter.
Do not parse formatted assembly or expose dependency types throughout the runtime.
Register effects, flags, faults, memory accesses, atomics, syscall dispatch and JIT
lowering remain our responsibility. Successfully decoding an instruction must not
automatically enable that instruction in our advertised guest CPU features.

Before adopting, pin revisions and test operand detail and invalid/truncated
encodings against the existing regressions and independent architecture controls.
Fetch guest instruction bytes through checked memory: x86 decoding must not
unconditionally read across an inaccessible page. Check ARM64 SIMD, atomics and
system-register instructions against actual workload binaries. Benchmark only
after correctness checks. Audit dependency licenses and unsafe implementation
boundaries with the selected versions.

Prove a tiny static guest for both architectures early, using these adapters, to
exercise the shared runtime design. Full AMD64 workload parity can still precede
full ARM64 parity. Decoder reuse reduces one portion of the port; it does not
replace the execution engine or the Linux compatibility layer.

### Execution backend choices

There are two materially different implementations behind the same public API:

1. **Worker-backed API:** the caller owns a Rust handle; execution happens in a
   separately launched worker. This can preserve the existing process model while
   keeping crashes and signal ownership out of the caller. It still uses process
   creation internally; it is not fully in-process embedding. Launch a fresh
   worker through spawn/exec, not by continuing arbitrary Rust code in a fork of
   the Tokio application. Worker-internal guest fork safety still needs auditing.
2. **In-process engine:** model guest processes, address spaces, shared memory,
   waits and descriptors explicitly. Guest fork must not blindly fork the host
   application. A scheduler and cancellable host I/O are substantial new work.

Use a worker-backed adapter for early integration/reference testing. Independently
prove the in-process model with an interpreter before promising direct embedding.
If the early fork/exec/thread/signal prototype cannot meet the semantics, retain
workers as an explicit backend rather than claiming that FFI has solved it.

## Implementation phases and acceptance gates

### 0. Freeze evidence and establish the comparison harness

- Record source hashes, local diff/untracked sources, configure options, external
  adapter versions and hashes of the exact successful C runtimes.
- Preserve existing artifacts; store new comparison runs separately.
- Classify the existing asm/function/probe tests by instruction, ABI, filesystem,
  process, JIT and experimental behavior. Some host-side C JIT probes need
  equivalent Rust tests; they are not portable guest tests.
- Establish C-interpreter and C-JIT results separately. Use native Linux AMD64
  controls where available: C Blink is a reference, not an infallible oracle.
- Record build time, peak RSS, output hashes, metadata and cleanup results.

Gate: a reproducible inventory identifies exactly what was required for the
successful builds and which known failures must remain visible.

### 1. Prove lifecycle and the hardest process decisions

- Add proposed API types, explicit resource ownership and run outcomes.
- Prototype guest memory, a minimal AMD64 interpreter/ELF path, write and exit.
- Run a tiny static guest that prints and exits; then run it again in the same
  context without terminating or changing the caller.
- Prove fork/exec/wait, basic guest threads, shared descriptors, signal delivery
  and timeout cancellation with focused fixtures before porting broad syscalls.
- If a C adapter is explored, extract frontend entry glue, handle exit traps in
  a C-only run loop and execute it in a worker initially. Do not expose Machine
  pointers as the public Rust API or enable concurrent C contexts prematurely.

Gate: repeated calls, failure then success, two different roots, blocked-I/O
cancellation and descendant cleanup have explicit verified behavior. Basic guest
process semantics are demonstrated, not left to the end of the CPU port.

### 2. Port AMD64 interpreter and Linux ABI in vertical slices

- Port decoder, registers, flags, integer operations and checked memory first.
- Implement ELF loading, initial stack/auxv, dynamic linker behavior and TLS.
- Expand syscall support alongside guests: static hello, BusyBox shell, then
  Debian shell/coreutils, package manager, Node and Bun.
- Port SIMD, floating point and atomics against the existing regression suite;
  include instruction faults, rounding modes, flags and page-boundary accesses.
- Implement VFS/procfs, credentials/metadata, futexes, clocks, networking,
  epoll/eventfd and process signals to the behavior demanded by these workloads.
- Reject unsupported features clearly. Keep diagnostics such as placeholder maps
  distinguishable from correct implementations; do not advertise unsupported ISA.

Gate: interpreter workloads and focused regressions pass, including the defects
fixed by PR #218. A shell booting alone does not count as builder compatibility.

### 3. Integrate the builder and reach artifact parity

- Introduce a Rust stage executor using the public API. Preserve current recipe
  parsing semantics, USER/ENV/WORKDIR, COPY, rootless metadata seeding and snapshots.
- Keep OCI downloading/assembly separate from guest execution.
- Replace the Python checkpoint execution path incrementally, retaining receipts,
  resume validation, logs, bounded deadlines and verified worker cleanup.
- Build Claude-base, Command Center, and final code-server from clean inputs on
  case-sensitive storage. Preserve CMD/ENTRYPOINT inheritance semantics.
- Run existing OCI/archive verifiers and compare filesystem content, UID/GID,
  modes, links, xattrs and runtime config. Account for generated state separately;
  do not require identical digests for nondeterministic build outputs.

Gate: all 26 current checkpoints and both target-image verifiers pass using Rust
execution, with no silent fallback to the C engine. State measured performance.

### 4. Add a Rust JIT without changing the API

- Select an emitter/lowering approach only after a small measured prototype.
- Preserve interpreter semantics as the reference for compiled blocks.
- Implement executable-memory permissions, Apple JIT requirements, instruction
  cache flushing, block lifetime, invalidation, self-modifying code and concurrent
  use. Guest exec must not invalidate another live process's code unexpectedly.
- Port the pool/lease, carry, MOVSD and CMPXCHG regressions and expand differential
  tests. Do not claim success just because interpreter fallback hides JIT failures.

Gate: the same workloads pass under JIT and performance is measured against both
the Rust interpreter and the frozen C runtimes.

### 5. Complete the broader Blink port and add ARM64 guests

Maintain a parity matrix for other hosts, legacy modes, debugger UI and build
configurations; port and validate each before calling the entire project ported.
Do not assume application-specific success establishes that matrix.

ARM64 guest support gets its own decoder/register state/instruction semantics,
ELF machine handling, syscall numbers/structures, TLS, signals and atomics. Reuse
architecture-independent Linux/VFS services. Begin with static guests, then a
dynamic shell and ARM64 versions of the build workloads. Existing AMD64 assembly
tests do not validate ARM64 semantics.

An ARM64 interpreter still interprets on Apple Silicon. Same-ISA native execution
is a separate optimization requiring controlled syscall interception, memory/TLS
and signal handling; it is not a consequence of writing the runtime in Rust.

## First implementation slice

Create the evidence manifest/comparison harness and a small Rust library with
`Blink::create`, `run` and `close`. Prove repeated static-guest execution, explicit
guest exit outcomes, root separation and resource cleanup. Then validate the
process-model prototype before expanding the port.

This produces a reviewable foundation and evidence for estimating the remaining
work. A credible completion date for full Blink parity cannot be inferred from
line count or this source review alone.
