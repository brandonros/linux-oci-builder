#!/usr/bin/env python3
"""Opt-in integration test using a prepared Debian rootfs with fakeroot/Python."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--blink", required=True)
    parser.add_argument("--rootfs", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = args.rootfs.resolve()
    args.output.mkdir(parents=True, exist_ok=False)
    with tempfile.TemporaryDirectory(prefix="capture-test-", dir=root / "tmp") as directory:
        fixture = Path(directory)
        (fixture / "removed").write_text("delete me\n")
        (fixture / "unchanged").write_text("keep me\n")
        guest = "/" + str(fixture.relative_to(root))
        runner = [sys.executable, str(Path(__file__).with_name("blink-checkpoint.py")),
                  "--blink", args.blink, "--rootfs", str(root),
                  "--snapshot-root", guest, "--workdir", guest, "--timeout", "90"]
        success = args.output / "success"
        subprocess.run(runner + ["--output", str(success), "--uid", "1000", "--gid", "1000",
            "--", "/bin/sh", "-ec",
            'test "$(id -u):$(id -g)" = 1000:1000; '
            "printf 'hello capture\n' > message; chown 1000:1000 message; "
            "chmod 640 message; ln message linked; ln -s message symbolic; rm removed"], check=True)
        report = json.loads((success / "result.json").read_text())
        assert report["capture_complete"] and report["exit_code"] == 0
        with tarfile.open(success / "layer.tar") as archive:
            entries = {m.name: m for m in archive}
            assert ".wh.removed" in entries and "unchanged" not in entries
            for name in ("message", "linked"):
                member = entries[name]
                assert (member.uid, member.gid, member.mode) == (1000, 1000, 0o640)
                assert archive.extractfile(member).read() == b"hello capture\n"
            assert entries["symbolic"].issym() and entries["symbolic"].linkname == "message"
        failed = args.output / "failed"
        result = subprocess.run(runner + ["--output", str(failed), "--", "/bin/sh", "-c", "exit 7"])
        assert result.returncode == 7 and not (failed / "layer.tar").exists()
        assert not json.loads((failed / "result.json").read_text()).get("capture_complete")
    print("PASS: guest identity, ownership, modes, links, deletion, unchanged files, failed-command rejection")


if __name__ == "__main__":
    main()
