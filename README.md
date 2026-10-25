# linux-oci-builder

Build Linux OCI images from Dockerfiles using userspace emulation.

Currently targets **macOS ARM64**, running AMD64 Linux build commands through
[Blink](https://github.com/jart/blink). No Docker daemon or booted Linux VM is
required. Other hosts and guest architectures are future work.

## What works

- Pull public container images into a local OCI image layout.
- Execute a supported subset of Dockerfile instructions with resumable checkpoints.
- Capture filesystem changes, including Linux ownership, modes, links and deletions.
- Assemble and verify derived OCI images.

The Claude-base and code-server Dockerfiles from the pinned agent-platform fixture
have completed builds and OCI exports on macOS ARM64. The project is experimental:
it is not yet a general-purpose Dockerfile replacement, and the Rust CLI currently
handles image pulls while Python scripts orchestrate builds.

## Getting started

Requires Rust, Python 3, and `bsdtar`. Building the runtime also requires a C
compiler, CMake, and GNU Make 4+.

```sh
git clone --recurse-submodules https://github.com/brandonros/linux-oci-builder.git
cd linux-oci-builder

# Pull an AMD64 Linux base image for Blink.
cargo run -- docker.io/library/alpine:3.22 \
  --platform linux/amd64 --output image
```

The output directory must not already exist. Image pulls verify blob sizes and
SHA-256 digests. Registry access currently uses anonymous authentication.

See [runtime setup](docs/runtime-setup.md) to build Blink. With a prepared runtime:

```sh
cargo run --example blink-suite -- \
  --blink ext/blink/o/blink/blink --filter 00-hello

python3 scripts/build-targets.py --help
```

Build outputs require a **case-sensitive filesystem**. The builder accepts explicit
base-image, Dockerfile, context, runtime and output paths; it does not yet provide
a single `build .` command. `examples/oci-assemble.rs` and `scripts/oci-diff.py`
provide standalone image assembly and filesystem changeset tools.

## Development

```sh
cargo test --all-targets
cargo clippy --all-targets -- -D warnings
python3 scripts/test-oci-diff.py
python3 scripts/test-build-targets.py
python3 scripts/test-compare-oci.py
```

Blink and epoll-shim are pinned submodules. All Blink runtime changes live on
`brandonros/blink` branch `fix/macos-linux-userspace`, backing
[upstream PR #218](https://github.com/jart/blink/pull/218). No patch application
is needed after cloning.
The epoll-shim fork includes the committed fix and regression test from
[upstream PR #64](https://github.com/jiixyj/epoll-shim/pull/64).
Only trusted build commands should be run: Blink's filesystem and virtual metadata
support are not a security sandbox. Linux compatibility and JIT correctness remain
incomplete; some validated build stages required interpreter mode.

- [Runtime setup and known limitations](docs/runtime-setup.md)
- [Reusable Rust runtime plan](docs/blink-rs-plan.md)
- [Historical build notes and validation evidence](docs/build-history.md)
