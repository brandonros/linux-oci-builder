#!/usr/bin/env python3
"""Create an OCI changeset from before/after filesystem tar snapshots.

Snapshots must contain guest ownership and modes (e.g. tar under fakeroot), not
the host's extracted-rootfs ownership. No archive is extracted on the host.
"""
import argparse
import copy
import hashlib
import os
from pathlib import Path, PurePosixPath
import tarfile
import tempfile


def name(value):
    p = PurePosixPath(value)
    if p.is_absolute() or ".." in p.parts:
        raise ValueError("Unsafe snapshot member: " + value)
    if any(part.startswith(".wh.") for part in p.parts):
        raise ValueError("Snapshots must not contain OCI whiteout names: " + value)
    return str(p)


class Snapshot:
    def __init__(self, archive):
        self.archive = archive
        self.members = {}
        self.hashes = {}
        for member in archive:
            key = name(member.name)
            if key in self.members:
                raise ValueError("Duplicate snapshot member: " + key)
            if member.issparse() or not (member.isfile() or member.isdir() or
                    member.issym() or member.islnk() or member.ischr() or
                    member.isblk() or member.isfifo()):
                raise ValueError("Unsupported snapshot entry: " + key)
            self.members[key] = member
            if member.isfile():
                digest = hashlib.sha256()
                with archive.extractfile(member) as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(chunk)
                self.hashes[key] = digest.hexdigest()
        # Validate hardlinks before constructing the output.
        self.groups = {}
        for key in self.members:
            for parent in PurePosixPath(key).parents:
                if str(parent) != "." and (str(parent) not in self.members or
                        not self.members[str(parent)].isdir()):
                    raise ValueError("Snapshot needs a directory entry for: " + str(parent))
            target = self.target(key)
            if self.members[target].isfile():
                self.groups.setdefault(target, set()).add(key)

    def target(self, key):
        seen = set()
        while self.members[key].islnk():
            if key in seen:
                raise ValueError("Cyclic snapshot hardlink: " + key)
            seen.add(key)
            key = name(self.members[key].linkname)
            if key not in self.members:
                raise ValueError("Missing snapshot hardlink target: " + key)
        if seen and not self.members[key].isfile():
            raise ValueError("Hardlink must target a regular file: " + key)
        return key

    def fingerprint(self, key):
        target = self.target(key)
        m = self.members[target]
        # Compare inode metadata, independent of which hardlink was archived first.
        pax = tuple(sorted((k, v) for k, v in m.pax_headers.items()
                           if k not in ("path", "linkpath", "size", "mtime", "atime", "ctime")))
        group = tuple(sorted(self.groups.get(target, ())))
        return (m.type, m.mode, m.uid, m.gid, m.mtime, m.size,
                m.linkname if m.issym() else "", m.devmajor, m.devminor,
                pax, self.hashes.get(target), group)


def create_diff(before_path, after_path, output):
    output = Path(output)
    if output.exists():
        raise ValueError("Output already exists: " + str(output))
    with tarfile.open(before_path, "r:*") as old, tarfile.open(after_path, "r:*") as new:
        before, after = Snapshot(old), Snapshot(new)
        changed = {key for key in after.members if key not in before.members or
                   after.fingerprint(key) != before.fingerprint(key)}
        # Re-emit complete hardlink groups so updates cannot leave links pointing
        # at a replaced inode from a lower layer.
        for group in after.groups.values():
            if group & changed:
                changed.update(group)
        removed = set(before.members) - set(after.members)
        # Changing entry type must remove the old object, especially directories.
        for key in before.members.keys() & after.members.keys():
            a = before.members[before.target(key)]
            b = after.members[after.target(key)]
            if a.type != b.type:
                removed.add(key)
        whiteouts = []
        for key in sorted(removed, key=lambda k: (len(PurePosixPath(k).parts), k)):
            if key == ".":
                raise ValueError("Cannot remove snapshot root")
            if not any(str(parent) in removed for parent in PurePosixPath(key).parents):
                whiteouts.append(key)
        output.parent.mkdir(parents=True, exist_ok=True)
        # Publish with a hardlink so an existing destination is never replaced.
        with tempfile.NamedTemporaryFile(dir=output.parent) as temporary:
            with tarfile.open(fileobj=temporary, mode="w", format=tarfile.PAX_FORMAT) as layer:
                for key in whiteouts:
                    p = PurePosixPath(key)
                    m = tarfile.TarInfo(str(p.parent / (".wh." + p.name)))
                    m.mode = 0
                    layer.addfile(m)
                # Data targets precede hardlinks, even if the input used forward links.
                order = sorted(changed, key=lambda k: (after.members[k].islnk(), len(PurePosixPath(k).parts), k))
                for key in order:
                    original = after.members[key]
                    m = copy.deepcopy(after.members[after.target(key)] if original.islnk() else original)
                    m.name = key
                    m.pax_headers = {k: v for k, v in m.pax_headers.items()
                                     if k not in ("path", "linkpath", "atime", "ctime")}
                    if original.islnk():
                        target = after.target(key)
                        m.type = tarfile.LNKTYPE
                        m.linkname = target
                        m.size = 0
                        m.pax_headers.pop("size", None)
                    stream = new.extractfile(original) if original.isfile() else None
                    try:
                        layer.addfile(m, stream)
                    finally:
                        if stream is not None:
                            stream.close()
            temporary.flush()
            os.link(temporary.name, output)
        return {"changed_entries": len(changed), "whiteouts": len(whiteouts)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--before", required=True, type=Path)
    parser.add_argument("--after", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    print(create_diff(args.before, args.after, args.output))


if __name__ == "__main__":
    main()
