#!/usr/bin/env python3
"""Run a Debian/Python checkpoint under Blink, preserving logs and exit status.

This is a diagnostic runner, not a sandbox or a Dockerfile builder. Guest code
can access host resources through Blink. Use only trusted build contexts.
"""
import argparse
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import shlex
import signal
import subprocess
import sys
import tempfile
import time


def capture_script(guest_tmp, snapshot_root, state, argv, uid, gid, env, workdir, tools_prefix=""):
    """Both snapshots share the command's fakeroot daemon and ownership map."""
    exclusions = [guest_tmp, state, "/proc", "/sys", "/dev", "/SystemRoot"]
    if tools_prefix:
        exclusions.append(tools_prefix)
    base = snapshot_root.rstrip("/")
    patterns = []
    for path in exclusions:
        if path.startswith(base + "/"):
            patterns.append("--exclude=." + path[len(base):])
    tar = ["/bin/tar", "--format=posix", "--numeric-owner", "--sort=name",
           "--one-file-system", *patterns]
    return (
        "import os, subprocess, sys\n"
        f"tar = {tar!r}\n"
        "def capture(label):\n"
        f"    subprocess.run(tar + ['-cf', {guest_tmp!r} + '/' + label + '.tar', "
        f"'-C', {snapshot_root!r}, '.'], check=True)\n"
        "capture('before')\n"
        f"os.setgid({gid!r})\nos.setuid({uid!r})\n"
        f"os.environ.update({env!r})\nos.chdir({workdir!r})\n"
        # env=None preserves fakeroot's libc environment updates.
        f"result = subprocess.run({argv!r})\n"
        "if result.returncode == 0:\n"
        "    os.setuid(0)\n    os.setgid(0)\n    os.chdir('/')\n"
        "    capture('after')\n"
        "sys.exit(result.returncode if result.returncode >= 0 else 128 - result.returncode)\n"
    )


def stop_escaped_workers(runtime, details=None):
    """setsid() can move guest descendants outside the runner's process group.

    Every invocation owns a unique immutable Blink executable path. Only kill
    processes whose command starts with that exact path, never a generic name.
    """
    prefix = str(runtime) + " "
    found = set()
    deadline = time.monotonic() + 5
    while True:
        live = {}
        for line in subprocess.check_output(["ps", "-ax", "-o", "pid=,command="], text=True).splitlines():
            parts = line.strip().split(None, 1)
            if len(parts) == 2 and parts[1].startswith(prefix):
                live[int(parts[0])] = parts[1]
        if not live:
            return sorted(found)
        if time.monotonic() >= deadline:
            raise RuntimeError("Guest workers remain after cleanup: " + repr(live))
        for pid, command in sorted(live.items()):
            if pid not in found and details is not None:
                details.append({"pid": pid, "command": command})
            found.add(pid)
            # Recheck identity in case a PID was reused after enumeration.
            result = subprocess.run(["ps", "-p", str(pid), "-o", "command="],
                                    capture_output=True, text=True)
            if result.returncode == 0 and result.stdout.strip().startswith(prefix):
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        time.sleep(0.05)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--blink", type=Path, required=True)
    parser.add_argument("--runtime-kind", choices=["c", "rust"], default="c")
    parser.add_argument("--rootfs", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--uid", type=int, default=0)
    parser.add_argument("--gid", type=int, default=0)
    parser.add_argument("--home", default="/root")
    parser.add_argument("--workdir", default="/")
    parser.add_argument("--timeout", type=float, default=900)
    parser.add_argument("--jit", action="store_true")
    parser.add_argument("--rootless-metadata", action="store_true", help="Use Blink's experimental syscall-level image metadata backend instead of fakeroot")
    parser.add_argument("--fault-trace", action="store_true", help="Enable bounded Blink fault diagnostics")
    parser.add_argument("--trace", action="store_true", help="Enable Blink syscall tracing for a focused diagnostic")
    parser.add_argument("--dns-tcp", action="store_true")
    parser.add_argument("--env", action="append", default=[])
    parser.add_argument("--fakeroot-state", default="/tmp/fakeroot.state")
    parser.add_argument("--fakeroot-prefix", default="", help="Guest prefix containing extracted fakeroot packages; excluded from snapshots")
    parser.add_argument("--snapshot-root", help="Capture this guest directory before/after a successful command and emit layer.tar; use / for the full rootfs")
    parser.add_argument("argv", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    argv = args.argv[1:] if args.argv[:1] == ["--"] else args.argv
    if not argv or args.timeout <= 0:
        parser.error("a command and a positive timeout are required")
    for value in [args.home, args.workdir, args.fakeroot_state]:
        if not value.startswith("/"):
            parser.error("home, workdir and fakeroot state must be guest absolute paths")
    if args.fakeroot_prefix:
        from pathlib import PurePosixPath
        prefix = PurePosixPath(args.fakeroot_prefix)
        if not prefix.is_absolute() or ".." in prefix.parts or str(prefix) in ("/", "//"):
            parser.error("fakeroot prefix must be a guest absolute subdirectory")
        args.fakeroot_prefix = str(prefix)
    guest_env = {"HOME": args.home}
    for item in args.env:
        key, separator, value = item.partition("=")
        if not separator or not key:
            parser.error("--env requires NAME=VALUE")
        guest_env[key] = value
    root = args.rootfs.resolve(strict=True)
    output = args.output.resolve()
    if output == root or root in output.parents:
        parser.error("output must be outside the guest rootfs")
    if args.snapshot_root is not None:
        from pathlib import PurePosixPath
        path = PurePosixPath(args.snapshot_root)
        if not path.is_absolute() or ".." in path.parts or str(path).startswith("//"):
            parser.error("snapshot root must be a normalized guest absolute path")
        args.snapshot_root = str(path)
    if args.runtime_kind == "rust":
        if not args.rootless_metadata or args.jit or args.trace or args.fault_trace or args.snapshot_root is not None:
            parser.error("Rust requires --rootless-metadata; JIT, trace and inline snapshots are unsupported")
        from rust_worker import RustWorker
        worker = RustWorker(args.blink, root, output.parent / (output.name + ".runtime"))
        try:
            worker.checkpoint(output, argv, args.uid, args.gid, args.home, args.workdir, guest_env, args.timeout)
        finally:
            worker.close()
        return
    # These runners serialize mutations of a rootfs and its fakeroot database.
    # Older ad-hoc scripts do not honor this lock and must be stopped separately.
    with (root.parent / (root.name + ".checkpoint.lock")).open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        output.mkdir(parents=True, exist_ok=False)
        runtime = output / "blink"
        shutil.copy2(args.blink.resolve(strict=True), runtime)
        manifest = {
            "rootfs": str(root), "argv": argv, "uid": args.uid, "gid": args.gid,
            "workdir": args.workdir, "env": guest_env,
            "fakeroot_state": args.fakeroot_state, "dns_tcp": args.dns_tcp,
            "mode": "jit" if args.jit else "interpreter",
            "trace": args.trace,
            "fault_trace": args.fault_trace,
            "metadata_backend": "blink-rootless" if args.rootless_metadata else "fakeroot",
            "blink_sha256": hashlib.sha256(runtime.read_bytes()).hexdigest(),
            "started": time.time(), "timeout_seconds": args.timeout,
            "snapshot_root": args.snapshot_root,
            "fakeroot_prefix": args.fakeroot_prefix,
        }
        report = output / "result.json"
        report.write_text(json.dumps(manifest, indent=2) + "\n")
        temporary_root = root / args.fakeroot_prefix.lstrip("/") / "tmp" if args.fakeroot_prefix else root / "tmp"
        temporary_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="checkpoint-", dir=temporary_root) as tmp:
            wrapper = Path(tmp) / "run.py"
            wrapper.write_text(
                "import os\n"
                f"os.setgid({args.gid!r})\nos.setuid({args.uid!r})\n"
                f"os.environ.update({guest_env!r})\n"
                f"os.chdir({args.workdir!r})\n"
                # fakeroot setuid/setgid update libc's environment directly;
                # passing Python's cached os.environ would discard those IDs.
                f"os.execvp({argv[0]!r}, {argv!r})\n"
            )
            guest_wrapper = "/" + str(wrapper.relative_to(root))
            interpreter = "/usr/bin/python3"
            if args.uid == 0 and args.gid == 0 and args.snapshot_root is None:
                # Fresh Debian and Node images have a shell but no Python.
                wrapper.write_text("#!/bin/sh\nset -e\ncd " + shlex.quote(args.workdir) +
                    "\nexec /usr/bin/env " + shlex.join([k + "=" + v for k, v in guest_env.items()]) +
                    " " + shlex.join(argv) + "\n")
                interpreter = "/bin/sh"
            if args.snapshot_root is not None:
                wrapper.write_text(capture_script(
                    str(Path(guest_wrapper).parent), args.snapshot_root,
                    args.fakeroot_state, argv, args.uid, args.gid, guest_env, args.workdir, args.fakeroot_prefix))
            env = {"BLINK_PREFIX": str(root), "HOME": "/root",
                   "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"}
            if args.fault_trace:
                env["BLINK_TRACE_FAULTS"] = "1"
            if args.rootless_metadata:
                env["BLINK_ROOTLESS"] = "1"
            if args.dns_tcp:
                env["RES_OPTIONS"] = "use-vc"
            fakeroot_options = []
            if args.fakeroot_prefix:
                fakeroot_options = ["-l", args.fakeroot_prefix + "/usr/lib/x86_64-linux-gnu/libfakeroot/libfakeroot-tcp.so",
                                    "-f", args.fakeroot_prefix + "/usr/bin/faked-tcp"]
            command = [str(runtime), "-m" if args.jit else "-jm", *(["-s"] if args.trace else []),
                       "-L", str(output / "blink.log"), args.fakeroot_prefix + "/usr/bin/fakeroot-tcp",
                       *fakeroot_options,
                       "-i", args.fakeroot_state, "-s", args.fakeroot_state,
                       "--", interpreter, guest_wrapper]
            if args.rootless_metadata:
                command = [str(runtime), "-m" if args.jit else "-jm", *(["-s"] if args.trace else []),
                           "-L", str(output / "blink.log"), interpreter, guest_wrapper]
            with (output / "stdout").open("wb") as out, (output / "stderr").open("wb") as err:
                process = subprocess.Popen(command, env=env, stdout=out, stderr=err,
                                           start_new_session=True)
                manifest["pid"] = process.pid
                report.write_text(json.dumps(manifest, indent=2) + "\n")
                try:
                    code = process.wait(timeout=args.timeout)
                    manifest["timed_out"] = False
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    code = process.wait()
                    manifest["timed_out"] = True
                except BaseException:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
                    raise
                finally:
                    manifest["finished"] = time.time()
                    manifest["exit_code"] = process.returncode
                    try:
                        manifest["escaped_workers"] = []
                        manifest["escaped_worker_pids"] = stop_escaped_workers(runtime, manifest["escaped_workers"])
                        manifest["cleanup_verified"] = True
                    except Exception as error:
                        manifest["cleanup_error"] = str(error)
                        print("Worker cleanup failed: " + str(error), file=sys.stderr)
                    report.write_text(json.dumps(manifest, indent=2) + "\n")
            if manifest.get("cleanup_error"):
                code = 1
            if args.snapshot_root is not None and code == 0:
                try:
                    if manifest.get("cleanup_error") or manifest.get("escaped_worker_pids"):
                        raise RuntimeError("Cannot publish snapshots with failed cleanup or surviving guest workers")
                    for label in ("before", "after"):
                        shutil.copyfile(Path(tmp) / (label + ".tar"), output / (label + ".tar"))
                    spec = importlib.util.spec_from_file_location("oci_diff", Path(__file__).with_name("oci-diff.py"))
                    diff = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(diff)
                    diff.create_diff(output / "before.tar", output / "after.tar", output / "layer.tar")
                    digest = hashlib.sha256()
                    with (output / "layer.tar").open("rb") as layer:
                        for chunk in iter(lambda: layer.read(1024 * 1024), b""):
                            digest.update(chunk)
                    manifest["layer_sha256"] = digest.hexdigest()
                    manifest["capture_complete"] = True
                except Exception as error:
                    manifest["capture_error"] = str(error)
                    code = 1
                report.write_text(json.dumps(manifest, indent=2) + "\n")
        print(f"Checkpoint exited {code}; evidence: {output}")
        return 124 if manifest["timed_out"] else code if code >= 0 else 128 - code


if __name__ == "__main__":
    sys.exit(main())
