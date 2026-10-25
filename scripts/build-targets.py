#!/usr/bin/env python3
"""Reproducible, resumable target Dockerfile checkpoints using Blink on macOS.

Supports the explicit instruction subset used by agent-platform's Claude-base
Dockerfile. Unsupported syntax fails rather than silently changing semantics.
The source context and OCI base are recorded and hashed before execution.
"""
import argparse
import hashlib
import gzip
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time

HERE = Path(__file__).resolve().parent
CONTROL = "/.image-forge"


def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def blob(layout, descriptor):
    algorithm, value = descriptor["digest"].split(":", 1)
    if algorithm != "sha256" or not re.fullmatch("[a-f0-9]{64}", value):
        raise ValueError("Unsupported blob digest")
    path = layout / "blobs" / algorithm / value
    if path.stat().st_size != descriptor["size"] or digest(path) != value:
        raise ValueError("Corrupt OCI blob: " + str(path))
    return path


def instructions(path):
    pending = ""
    for line in path.read_text().splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        line = line.strip()
        if line.endswith("\\"):
            pending += line[:-1] + " "
        else:
            instruction, value = (pending + line).split(None, 1)
            yield instruction.upper(), value
            pending = ""
    if pending:
        raise ValueError("Unterminated Dockerfile continuation")


def guest_path(root, name):
    """Resolve parent symlinks with guest-root semantics, never host-root semantics."""
    parts = list(PurePosixPath(name).parts)
    if not parts or parts == ["."]:
        return root
    pending, resolved, links = parts[:-1], [], 0
    while pending:
        part = pending.pop(0)
        if part in ("", ".", "/"):
            continue
        if part == "..":
            if resolved:
                resolved.pop()
            continue
        candidate = root.joinpath(*resolved, part)
        if candidate.is_symlink():
            links += 1
            if links > 40:
                raise ValueError("Symlink loop in image")
            target = os.readlink(candidate)
            if target.startswith("/"):
                resolved = []
            pending = list(PurePosixPath(target).parts) + pending
        else:
            resolved.append(part)
    return root.joinpath(*resolved, parts[-1])


def remove_entry(path):
    if path.is_symlink() or not path.is_dir():
        path.unlink(missing_ok=True)
    else:
        shutil.rmtree(path)


def apply_layer(archive, root, owners):
    """Apply whiteouts before entries; bsdtar enforces safe archive extraction."""
    with tarfile.open(archive) as tar:
        members = tar.getmembers()
        for member in members:
            name = PurePosixPath(member.name)
            if name.is_absolute() or ".." in name.parts:
                raise ValueError("Unsafe image member: " + member.name)
            if member.islnk() and (PurePosixPath(member.linkname).is_absolute() or ".." in PurePosixPath(member.linkname).parts):
                raise ValueError("Unsafe image hardlink")
        for member in members:
            name = PurePosixPath(member.name)
            if not name.name.startswith(".wh."):
                continue
            if name.name == ".wh..wh..opq":
                directory = guest_path(root, str(name.parent / "placeholder")).parent
                if directory.is_dir():
                    for child in directory.iterdir():
                        remove_entry(child)
                removed = str(name.parent).rstrip("/") + "/"
                for key in list(owners):
                    if key.startswith(removed) or name.parent == PurePosixPath("."):
                        del owners[key]
            else:
                target = name.with_name(name.name[4:])
                if target.name in ("", ".", ".."):
                    raise ValueError("Invalid whiteout")
                remove_entry(guest_path(root, str(target)))
                for key in list(owners):
                    if key == str(target) or key.startswith(str(target) + "/"):
                        del owners[key]
        with tempfile.NamedTemporaryFile(dir=root.parent, suffix=".tar") as filtered:
            with tarfile.open(fileobj=filtered, mode="w", format=tarfile.PAX_FORMAT) as output:
                for member in members:
                    name = PurePosixPath(member.name)
                    if name.name.startswith(".wh."):
                        continue
                    owners[str(name)] = [member.uid, member.gid]
                    target = guest_path(root, str(name))
                    if target != root and (target.exists() or target.is_symlink()):
                        if (target.is_dir() and not target.is_symlink()) != member.isdir():
                            remove_entry(target)
                            for key in list(owners):
                                if key.startswith(str(name) + "/"):
                                    del owners[key]
                    output.addfile(member, tar.extractfile(member) if member.isfile() else None)
            filtered.flush()
            subprocess.run(["bsdtar", "-xpf", filtered.name, "--no-same-owner", "-C", str(root)], check=True)


def require_case_sensitive(directory):
    with tempfile.TemporaryDirectory(prefix=".case-check-", dir=directory) as temporary:
        probe = Path(temporary)
        (probe / "lower").write_bytes(b"")
        if (probe / "LOWER").exists():
            raise ValueError("Build output must use a case-sensitive filesystem; "
                             "Linux paths such as sys/Sys must remain distinct. "
                             "Use a case-sensitive APFS volume or disk image on macOS.")


class Build:
    def __init__(self, args):
        self.args = args
        self.output = args.output.resolve()
        self.root = self.output / "rootfs"
        self.step = 0
        self.rust_worker = None
        self.config = {}
        self.user = "root"
        self.workdir = "/"
        stages = []
        for op, value in instructions(args.dockerfile):
            if op == "FROM":
                words = shlex.split(value)
                if len(words) not in (1, 3) or (len(words) == 3 and words[1].upper() != "AS"):
                    raise ValueError("Unsupported FROM syntax")
                stages.append((words[2] if len(words) == 3 else str(len(stages)), words[0], []))
            elif stages:
                stages[-1][2].append((op, value))
            elif op != "ARG":
                raise ValueError("Instruction before FROM")
        selected = [stage for stage in stages if stage[0] == args.stage] if args.stage else stages[-1:]
        if len(selected) != 1:
            raise ValueError("Select a valid Dockerfile stage")
        self.stage, self.source, self.recipe = selected[0]
        self.stage_roots = {}
        for binding in args.stage_root:
            name, separator, value = binding.partition("=")
            if not separator or not name:
                raise ValueError("stage-root requires NAME=ROOTFS")
            stage_root = Path(value).resolve(strict=True)
            if not (stage_root.parent / "complete.json").is_file():
                raise ValueError("COPY source stage has no completed build receipt")
            self.stage_roots[name] = stage_root

    def run(self, label, argv, uid=0, gid=0, home="/root", workdir="/", env=None):
        self.step += 1
        evidence = self.output / (f"{self.step:03}-" + label)
        command = [sys.executable, str(HERE / "blink-checkpoint.py"),
                   "--blink", str(self.args.blink.resolve()), "--rootfs", str(self.root),
                   "--output", str(evidence), "--fakeroot-prefix", CONTROL,
                   "--fakeroot-state", CONTROL + "/state", "--dns-tcp",
                   "--uid", str(uid), "--gid", str(gid), "--home", home,
                   "--workdir", workdir, "--timeout", str(self.args.timeout)]
        if getattr(self.args, "runtime_kind", "c") == "rust":
            command += ["--runtime-kind", "rust"]
        if not self.args.no_jit:
            command.append("--jit")
        if self.args.rootless_metadata:
            command.append("--rootless-metadata")
        if self.args.fault_trace:
            command.append("--fault-trace")
        for key, value in (env or {}).items():
            command += ["--env", key + "=" + value]
        command += ["--", *argv]
        signature = hashlib.sha256(json.dumps(command).encode()).hexdigest()
        receipt = evidence / "build-step.json"
        if evidence.exists():
            if receipt.exists():
                recorded = json.loads(receipt.read_text())
                previous = list(command)
                if self.args.previous_blink:
                    previous[previous.index("--blink") + 1] = str(self.args.previous_blink.resolve())
                candidates = [command, previous]
                # Completed commands need not rerun when execution/diagnostic
                # mode changes for a later failed checkpoint.
                for option in ("--fault-trace", "--jit"):
                    for candidate in list(candidates):
                        other_mode = list(candidate)
                        if option in other_mode:
                            other_mode.remove(option)
                        elif option == "--jit":
                            other_mode.insert(other_mode.index("--timeout") + 2, option)
                        else:
                            position = (other_mode.index("--rootless-metadata") + 1
                                        if "--rootless-metadata" in other_mode
                                        else other_mode.index("--timeout") + 2 + int("--jit" in other_mode))
                            other_mode.insert(position, option)
                        candidates.append(other_mode)
                valid = [{"signature": hashlib.sha256(json.dumps(c).encode()).hexdigest(), "complete": True}
                         for c in candidates]
                if recorded in valid:
                    print("RESUME", evidence.name, flush=True)
                    return
            raise ValueError("Incomplete checkpoint requires inspection before retry: " + str(evidence))
        print("RUN", evidence.name, shlex.join(argv), flush=True)
        if getattr(self.args, "runtime_kind", "c") == "rust":
            from rust_worker import RustWorker
            if self.rust_worker is None:
                self.rust_worker = RustWorker(self.args.blink, self.root, self.output / "runtime")
                if self.rust_worker.digest != getattr(self, "runtime_digest", self.rust_worker.digest):
                    self.close()
                    raise ValueError("Rust runtime changed after build provenance was recorded")
            self.rust_worker.checkpoint(evidence, argv, uid, gid, home, workdir, env, self.args.timeout)
        else:
            subprocess.run(command, check=True)
        report = json.loads((evidence / "result.json").read_text())
        # RUN descendants are terminated before the later snapshot/export step.
        # Older receipts only recorded attempted kills, not verified termination.
        if report.get("cleanup_error") or (report.get("escaped_worker_pids") and
                                           not report.get("cleanup_verified")):
            raise ValueError("Guest worker cleanup is unverified: " + str(evidence))
        receipt.write_text(json.dumps({"signature": signature, "complete": True}) + "\n")

    def close(self):
        if self.rust_worker is not None:
            self.rust_worker.close()
            self.rust_worker = None

    def prepare(self):
        base = self.args.base.resolve()
        index = json.loads((base / "index.json").read_text())
        manifest_path = blob(base, index["manifests"][0])
        manifest = json.loads(manifest_path.read_text())
        config = json.loads(blob(base, manifest["config"]).read_text())
        if (config["os"], config["architecture"]) != ("linux", "amd64"):
            raise ValueError("Blink requires linux/amd64")
        layers = [blob(base, layer) for layer in manifest["layers"]]
        if len(layers) != len(config["rootfs"]["diff_ids"]):
            raise ValueError("Base DiffID count mismatch")
        for archive, descriptor, expected in zip(layers, manifest["layers"], config["rootfs"]["diff_ids"]):
            h = hashlib.sha256()
            opener = gzip.open if descriptor["mediaType"].endswith("gzip") else open
            with opener(archive, "rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    h.update(chunk)
            if expected != "sha256:" + h.hexdigest():
                raise ValueError("Base uncompressed DiffID mismatch")
        self.config = dict(config.get("config", {}))
        self.user = self.config.get("User") or "root"
        self.workdir = self.config.get("WorkingDir") or "/"
        context = self.args.context.resolve()
        context_hashes = {str(p.relative_to(context)): digest(p) for p in sorted(context.rglob("*"))
                          if p.is_file() and ".git" not in p.relative_to(context).parts and "node_modules" not in p.relative_to(context).parts}
        provenance = {"base_manifest": "sha256:" + digest(manifest_path),
                      "dockerfile": digest(self.args.dockerfile), "context": context_hashes,
                      "blink": digest(self.args.blink),
                      "fakeroot_packages": {p.name: digest(p) for p in self.args.fakeroot_deb}}
        self.runtime_digest = provenance["blink"]
        if getattr(self.args, "runtime_kind", "c") == "rust":
            provenance["runtime_kind"] = "rust-worker-v1"
        if self.args.rootless_metadata:
            provenance["metadata_backend"] = "blink-rootless-v1"
        if self.args.stage:
            provenance["stage"] = self.args.stage
        if self.stage_roots:
            provenance["stage_sources"] = {}
            for op, value in self.recipe:
                parts = shlex.split(value)
                if op == "COPY" and parts[0].startswith("--from="):
                    source_root = self.stage_roots[parts[0][7:]]
                    for source in parts[1:-1]:
                        path = guest_path(source_root, source.lstrip("/"))
                        provenance["stage_sources"][parts[0] + ":" + source] = digest(path)
        if self.source in ("debian:trixie-slim", "node:22-bookworm-slim"):
            expected = "docker.io/library/" + self.source
            actual = index["manifests"][0].get("annotations", {}).get("org.opencontainers.image.ref.name")
            if actual != expected:
                raise ValueError("FROM does not match supplied base: " + str(actual))
        elif self.source not in ("claude-base:${BASE_TAG}", "${BASE_IMAGE}:${BASE_TAG}"):
            raise ValueError("Unsupported base binding: " + self.source)
        self.output.mkdir(parents=True, exist_ok=True)
        require_case_sensitive(self.output)
        record = self.output / "inputs.json"
        if record.exists():
            original = json.loads(record.read_text())
            if original != provenance:
                previous = dict(provenance)
                if self.args.previous_blink:
                    previous["blink"] = digest(self.args.previous_blink)
                if not self.args.previous_blink or original != previous:
                    raise ValueError("Build inputs changed; use a fresh output directory")
                history = self.output / "runtime-upgrades.json"
                upgrades = json.loads(history.read_text()) if history.exists() else []
                upgrades.append({"previous": previous["blink"], "replacement": provenance["blink"], "time": time.time()})
                history.write_text(json.dumps(upgrades, indent=2) + "\n")
                record.write_text(json.dumps(provenance, indent=2) + "\n")
        else:
            record.write_text(json.dumps(provenance, indent=2) + "\n")
        ready = self.output / "extracted.json"
        owners = {}
        if not ready.exists():
            self.root.mkdir(exist_ok=False)
            for archive in layers:
                apply_layer(archive, self.root, owners)
            control = self.root / CONTROL.lstrip("/")
            control.mkdir()
            for package in self.args.fakeroot_deb:
                listing = subprocess.check_output(["ar", "-t", str(package)], text=True).splitlines()
                data = next(x for x in listing if x.startswith("data.tar"))
                archive = control / "package.tar"
                with archive.open("wb") as stream:
                    subprocess.run(["ar", "-p", str(package), data], stdout=stream, check=True)
                subprocess.run(["bsdtar", "-xpf", str(archive), "-C", str(control), "--no-same-owner"], check=True)
                archive.unlink()
            (self.root / "tmp").mkdir(exist_ok=True)
            ready.write_text(json.dumps(owners))
        owners = json.loads(ready.read_text())
        # Rootless host extraction strips set-id bits. Reconstruct them in the
        # guest metadata after ownership seeding, before taking the baseline.
        modes = {}
        for archive in layers:
            with tarfile.open(archive) as tar:
                for member in tar:
                    modes[str(PurePosixPath(member.name))] = member.mode if not member.issym() else None
        groups = {}
        virtual = ("dev", "proc", "sys", "SystemRoot", CONTROL.lstrip("/"))
        def excluded_metadata(name):
            return any(name == prefix or name.startswith(prefix + "/") for prefix in virtual)
        for name, (uid, gid) in owners.items():
            if self.args.rootless_metadata and excluded_metadata(name):
                continue
            if (uid, gid) != (0, 0):
                groups.setdefault((uid, gid), []).append("/" + name)
        shell = "set -e\n"
        for (uid, gid), paths in groups.items():
            for start in range(0, len(paths), 100):
                shell += shlex.join(["chown", "-h", f"{uid}:{gid}", "--", *paths[start:start + 100]]) + "\n"
        for name, mode in sorted(modes.items()):
            if self.args.rootless_metadata and excluded_metadata(name):
                continue
            if name in owners and mode is not None and (self.args.rootless_metadata or mode & 0o6000):
                shell += shlex.join(["chmod", oct(mode)[2:], "--", "/" + name]) + "\n"
        if self.args.rootless_metadata:
            seed = self.root / CONTROL.lstrip("/") / "base-metadata.sh"
            seed.write_text(shell)
            self.run("base-ownership", ["/bin/sh", "-e", CONTROL + "/base-metadata.sh"])
        else:
            self.run("base-ownership", ["/bin/sh", "-ec", shell])
        self.snapshot("before")
        # DNS injection is build infrastructure and restored before final capture.
        resolver = self.root / "etc/resolv.conf"
        backup = self.output / "resolv-backup.json"
        if not backup.exists():
            backup.write_text(json.dumps({"content": resolver.read_text() if resolver.exists() else None}))
        shutil.copyfile("/etc/resolv.conf", resolver)

    def snapshot(self, label):
        path = CONTROL + "/" + label + ".tar"
        command = ["/bin/tar", "--format=posix", "--numeric-owner", "--sort=name", "--one-file-system",
                   "--exclude=./.image-forge", "--exclude=./SystemRoot", "--exclude=./proc",
                   "--exclude=./sys", "--exclude=./dev", "-cf", path, "-C", "/", "."]
        self.run("snapshot-" + label, command)
        destination = self.output / (label + ".tar")
        source = self.root / path.lstrip("/")
        if source.exists():
            source.rename(destination)
        if not destination.exists():
            raise ValueError("Missing snapshot: " + str(destination))

    def identity(self):
        if self.user in ("root", "0"):
            return 0, 0, "/root"
        for line in (self.root / "etc/passwd").read_text().splitlines():
            name, _, uid, gid, _, home, _ = line.split(":")
            if name == self.user or uid == self.user:
                return int(uid), int(gid), home
        raise ValueError("Unknown USER: " + self.user)

    def execute(self):
        env = dict(x.split("=", 1) for x in self.config.get("Env", []))
        cmd_in_stage = False
        for op, value in self.recipe:
            if op == "RUN":
                if value.startswith(("[", "--")):
                    raise ValueError("Only shell RUN is implemented")
                uid, gid, home = self.identity()
                self.run("run", ["/bin/sh", "-c", value], uid, gid, home, self.workdir, env)
            elif op == "USER":
                self.user = value
                self.config["User"] = value
            elif op == "WORKDIR":
                self.workdir = str(PurePosixPath(self.workdir) / value)
                uid, gid, _ = self.identity()
                self.run("workdir", ["/bin/sh", "-ec", shlex.join(["mkdir", "-p", self.workdir]) +
                         " && " + shlex.join(["chown", f"{uid}:{gid}", self.workdir])])
                self.config["WorkingDir"] = self.workdir
            elif op == "ENV":
                for item in shlex.split(value):
                    key, sep, val = item.partition("=")
                    if not sep:
                        raise ValueError("Only ENV NAME=value is implemented")
                    env[key] = re.sub(r"\$(\w+)|\$\{(\w+)\}", lambda m: env.get(m[1] or m[2], ""), val)
                self.config["Env"] = [k + "=" + v for k, v in env.items()]
            elif op == "COPY":
                self.copy(value)
            elif op in ("ENTRYPOINT", "CMD"):
                argv = json.loads(value)
                if not isinstance(argv, list) or not all(isinstance(x, str) for x in argv):
                    raise ValueError("Only JSON-array runtime commands are implemented")
                self.config["Entrypoint" if op == "ENTRYPOINT" else "Cmd"] = argv
                if op == "CMD":
                    cmd_in_stage = True
                elif not cmd_in_stage:
                    # Assembly overlays the base config: omission would retain its CMD.
                    self.config["Cmd"] = None
            elif op == "EXPOSE":
                for port in value.split():
                    self.config.setdefault("ExposedPorts", {})[port if "/" in port else port + "/tcp"] = {}
            else:
                raise ValueError("Unsupported Dockerfile instruction: " + op)

    def copy(self, value):
        parts = shlex.split(value)
        source_root = self.args.context
        from_stage = parts[0].startswith("--from=")
        if from_stage:
            source_root = self.stage_roots[parts.pop(0)[7:]]
        if len(parts) < 2 or parts[0].startswith("--"):
            raise ValueError("Unsupported COPY syntax")
        destination = str(PurePosixPath(self.workdir) / parts[-1])
        sources = []
        for value in parts[:-1]:
            source = guest_path(source_root, value.lstrip("/")) if from_stage else source_root / value
            source = source.resolve(strict=True)
            if source_root.resolve() not in source.parents:
                raise ValueError("COPY source escapes its context")
            sources.append(source)
        is_directory = parts[-1].endswith("/") or any(source.is_dir() for source in sources)
        if len(sources) > 1 and not is_directory:
            raise ValueError("Multiple COPY sources need a directory destination")
        archive_name = None if is_directory else PurePosixPath(destination).name
        if not is_directory:
            destination = str(PurePosixPath(destination).parent)
        archive = self.root / CONTROL.lstrip("/") / "copy.tar"
        with tarfile.open(archive, "w", format=tarfile.PAX_FORMAT) as tar:
            def normalize(member):
                member.uid = member.gid = 0
                member.uname = member.gname = ""
                return member
            for source in sources:
                if source.is_dir():
                    for child in sorted(source.iterdir()):
                        tar.add(child, arcname=child.name, filter=normalize)
                else:
                    tar.add(source, arcname=archive_name or source.name, filter=normalize)
        self.run("copy", ["/bin/sh", "-ec", shlex.join(["mkdir", "-p", destination]) + " && " +
                          shlex.join(["tar", "-xpf", CONTROL + "/copy.tar", "-C", destination])])
        archive.unlink()

    def export(self):
        backup = json.loads((self.output / "resolv-backup.json").read_text())["content"]
        resolver = self.root / "etc/resolv.conf"
        if backup is None:
            resolver.unlink(missing_ok=True)
        else:
            resolver.write_text(backup)
        self.snapshot("after")
        layer = self.output / "layer.tar"
        if not layer.exists():
            spec = importlib.util.spec_from_file_location("oci_diff", HERE / "oci-diff.py")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            module.create_diff(self.output / "before.tar", self.output / "after.tar", layer)
        config = self.output / "config.json"
        config.write_text(json.dumps(self.config, indent=2) + "\n")
        image = self.output / "image"
        if not image.exists():
            subprocess.run(["cargo", "run", "--example", "oci-assemble", "--", "--base", str(self.args.base),
                            "--layer", str(layer), "--config", str(config), "--output", str(image)], check=True)
        (self.output / "complete.json").write_text(json.dumps({"image": str(image), "layer_sha256": digest(layer)}) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--blink", type=Path, required=True)
    parser.add_argument("--runtime-kind", choices=["c", "rust"], default="c", help="Rust uses blink-worker with one persistent context per stage")
    parser.add_argument("--previous-blink", type=Path, help="Explicitly upgrade a resumed build runtime; previous completed checkpoint receipts are retained")
    parser.add_argument("--no-jit", action="store_true", help="Use Blink interpreter for remaining checkpoints; completed checkpoints retain their recorded mode")
    parser.add_argument("--fault-trace", action="store_true", help="Enable bounded Blink fault diagnostics for remaining checkpoints")
    parser.add_argument("--base", type=Path, required=True)
    parser.add_argument("--context", type=Path, required=True)
    parser.add_argument("--dockerfile", type=Path, required=True)
    parser.add_argument("--fakeroot-deb", type=Path, action="append", default=[])
    parser.add_argument("--rootless-metadata", action="store_true", help="Use Blink metadata instead of fakeroot for build commands and export")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout", type=int, default=1800)
    parser.add_argument("--stage", help="Select a named build stage; defaults to final stage")
    parser.add_argument("--stage-root", action="append", default=[], help="Bind COPY --from using NAME=ROOTFS of a completed stage")
    args = parser.parse_args()
    if not args.rootless_metadata and not args.fakeroot_deb:
        parser.error("provide --fakeroot-deb packages or --rootless-metadata")
    if args.runtime_kind == "rust" and (not args.rootless_metadata or not args.no_jit or args.fault_trace):
        parser.error("Rust requires --rootless-metadata --no-jit and does not support --fault-trace")
    build = Build(args)
    try:
        build.prepare()
        build.execute()
        build.export()
    finally:
        build.close()


if __name__ == "__main__":
    main()
