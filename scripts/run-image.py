#!/usr/bin/env python3
"""Verify, unpack and run a linux/amd64 OCI image through Blink on macOS.

Uses fakeroot ownership bookkeeping, not Linux credential enforcement. Bootstrap
tools live under /.image-forge in the disposable runtime root, outside the image.
"""
import argparse
import gzip
import hashlib
import importlib.util
import json
from pathlib import Path, PurePosixPath
import shlex
import shutil
import subprocess
import sys

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("build_targets", HERE / "build-targets.py")
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


def verify(layout):
    index = json.loads((layout / "index.json").read_text())
    if len(index["manifests"]) != 1:
        raise ValueError("Select a single-platform OCI layout")
    manifest = json.loads(builder.blob(layout, index["manifests"][0]).read_text())
    config = json.loads(builder.blob(layout, manifest["config"]).read_text())
    if (config["os"], config["architecture"]) != ("linux", "amd64"):
        raise ValueError("Blink requires linux/amd64")
    if len(manifest["layers"]) != len(config["rootfs"]["diff_ids"]):
        raise ValueError("DiffID count mismatch")
    layers = []
    for descriptor, diffid in zip(manifest["layers"], config["rootfs"]["diff_ids"]):
        archive = builder.blob(layout, descriptor)
        media = descriptor["mediaType"]
        if not (media.endswith(".tar") or media.endswith("gzip")):
            raise ValueError("Unsupported layer encoding: " + media)
        h = hashlib.sha256()
        with (gzip.open if media.endswith("gzip") else open)(archive, "rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                h.update(chunk)
        if diffid != "sha256:" + h.hexdigest():
            raise ValueError("Uncompressed layer digest mismatch")
        layers.append(archive)
    return index["manifests"][0]["digest"], config, layers


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", type=Path, required=True)
    parser.add_argument("--verify-only", action="store_true")
    parser.add_argument("--blink", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--fakeroot-deb", type=Path, action="append", default=[])
    parser.add_argument("--timeout", type=int, default=3600)
    parser.add_argument("--no-jit", action="store_true")
    parser.add_argument("--env", action="append", default=[])
    parser.add_argument("--command", action="store_true", help="Replace image entrypoint as well as Cmd")
    parser.add_argument("argv", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    manifest, document, layers = verify(args.image.resolve())
    print("Verified manifest, blobs and DiffIDs:", manifest, flush=True)
    if args.verify_only:
        return
    if not args.blink or not args.output or not args.fakeroot_deb:
        parser.error("running requires --blink, --output, and fakeroot bootstrap packages")
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    root = output / "rootfs"; root.mkdir()
    owners, modes = {}, {}
    for archive in layers:
        builder.apply_layer(archive, root, owners)
        with builder.tarfile.open(archive) as tar:
            for member in tar:
                modes[str(PurePosixPath(member.name))] = 0 if member.issym() else member.mode
    control = root / ".image-forge"; control.mkdir()
    for package in args.fakeroot_deb:
        listing = subprocess.check_output(["ar", "-t", str(package)], text=True).splitlines()
        data = next(x for x in listing if x.startswith("data.tar"))
        archive = control / "package.tar"
        with archive.open("wb") as stream:
            subprocess.run(["ar", "-p", str(package), data], stdout=stream, check=True)
        subprocess.run(["bsdtar", "-xpf", str(archive), "--no-same-owner", "-C", str(control)], check=True)
        archive.unlink()
    (root / "tmp").mkdir(exist_ok=True)
    shutil.copyfile("/etc/resolv.conf", root / "etc/resolv.conf")
    prefix = [sys.executable, str(HERE / "blink-checkpoint.py"), "--blink", str(args.blink.resolve()),
              "--rootfs", str(root), "--fakeroot-prefix", "/.image-forge", "--fakeroot-state", "/.image-forge/state",
              "--dns-tcp", "--timeout", str(args.timeout)]
    if not args.no_jit:
        prefix.append("--jit")
    groups = {}
    for name, (uid, gid) in owners.items():
        if (uid, gid) != (0, 0):
            groups.setdefault((uid, gid), []).append("/" + name)
    shell = "set -e\n"
    for (uid, gid), paths in groups.items():
        for start in range(0, len(paths), 100):
            shell += shlex.join(["chown", "-h", f"{uid}:{gid}", "--", *paths[start:start + 100]]) + "\n"
    for name, mode in sorted(modes.items()):
        if name in owners and mode & 0o6000:
            shell += shlex.join(["chmod", oct(mode)[2:], "--", "/" + name]) + "\n"
    # A file avoids host ARG_MAX for large image ownership maps.
    (control / "seed.sh").write_text(shell)
    subprocess.run(prefix + ["--output", str(output / "ownership"), "--", "/bin/sh", "/.image-forge/seed.sh"], check=True)
    config = document.get("config", {})
    user = config.get("User") or "root"
    uid = gid = 0; home = "/root"
    if user not in ("root", "0"):
        for line in (root / "etc/passwd").read_text().splitlines():
            name, _, u, g, _, h, _ = line.split(":")
            if user in (name, u):
                uid, gid, home = int(u), int(g), h
                break
        else:
            raise ValueError("Unsupported or unknown image User: " + user)
    supplied = args.argv[1:] if args.argv[:1] == ["--"] else args.argv
    argv = supplied if args.command else (config.get("Entrypoint") or []) + (supplied or config.get("Cmd") or [])
    if not argv:
        raise ValueError("Image has no command; supply one after --")
    command = prefix + ["--output", str(output / "runtime"), "--uid", str(uid), "--gid", str(gid),
                        "--home", home, "--workdir", config.get("WorkingDir") or "/"]
    for value in config.get("Env", []) + args.env:
        command += ["--env", value]
    command += ["--", *argv]
    (output / "image.json").write_text(json.dumps({"manifest": manifest, "config": config, "argv": argv}, indent=2) + "\n")
    print("Runtime output:", output / "runtime/stdout", flush=True)
    return subprocess.run(command).returncode


if __name__ == "__main__":
    sys.exit(main())
