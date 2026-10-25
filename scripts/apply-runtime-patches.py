#!/usr/bin/env python3
"""Apply the recorded runtime changes to initialized, pinned submodules."""
from pathlib import Path
import subprocess


def main():
    root = Path(__file__).resolve().parent.parent
    for patch in sorted((root / "patches").glob("*.patch")):
        repo = root / "ext" / patch.stem
        if not (repo / ".git").exists():
            raise SystemExit("Initialize dependencies with git submodule update --init --recursive")
        command = ["git", "-C", str(repo), "apply"]
        applied = subprocess.run(command + ["--reverse", "--check", str(patch)],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if applied.returncode == 0:
            print(f"{patch.stem}: already applied")
            continue
        subprocess.run(command + ["--check", str(patch)], check=True)
        subprocess.run(command + [str(patch)], check=True)
        print(f"{patch.stem}: applied")


if __name__ == "__main__":
    main()
