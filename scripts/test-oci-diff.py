import importlib.util
import io
import os
from pathlib import Path
import shutil
import tarfile
import tempfile
import unittest

spec = importlib.util.spec_from_file_location("oci_diff", Path(__file__).with_name("oci-diff.py"))
diff = importlib.util.module_from_spec(spec)
spec.loader.exec_module(diff)


def snapshot(path, entries):
    with tarfile.open(path, "w", format=tarfile.PAX_FORMAT) as tar:
        for name, kind, value, mode, uid in entries:
            m = tarfile.TarInfo(name)
            m.mode, m.uid, m.gid, m.mtime = mode, uid, uid, 1
            m.type = kind
            if kind in (tarfile.LNKTYPE, tarfile.SYMTYPE):
                m.linkname = value
            elif kind == tarfile.REGTYPE:
                m.size = len(value)
            tar.addfile(m, io.BytesIO(value) if kind == tarfile.REGTYPE else None)


class ChangesetTests(unittest.TestCase):
    def test_deletions_types_links_content_and_metadata(self):
        with tempfile.TemporaryDirectory() as temp:
            p = Path(temp)
            before = [
                ("keep", tarfile.REGTYPE, b"same", 0o644, 0),
                ("gone", tarfile.DIRTYPE, "", 0o755, 0),
                ("gone/child", tarfile.REGTYPE, b"delete", 0o644, 0),
                ("swap", tarfile.DIRTYPE, "", 0o755, 0),
                ("swap/child", tarfile.REGTYPE, b"delete", 0o644, 0),
                ("a", tarfile.REGTYPE, b"old", 0o644, 1000),
                ("b", tarfile.LNKTYPE, "a", 0o644, 1000),
                ("metadata", tarfile.REGTYPE, b"unchanged", 0o644, 0),
                ("link", tarfile.SYMTYPE, "/old", 0o777, 0),
            ]
            after = [
                before[0],
                ("swap", tarfile.REGTYPE, b"replacement", 0o600, 1000),
                ("a", tarfile.REGTYPE, b"new", 0o644, 1000),
                ("b", tarfile.LNKTYPE, "a", 0o644, 1000),
                ("metadata", tarfile.REGTYPE, b"unchanged", 0o755, 1000),
                ("link", tarfile.SYMTYPE, "/new", 0o777, 0),
            ]
            snapshot(p / "before", before)
            snapshot(p / "after", after)
            result = diff.create_diff(p / "before", p / "after", p / "delta")
            self.assertEqual(result, {"changed_entries": 5, "whiteouts": 2})
            with tarfile.open(p / "delta") as t:
                names = t.getnames()
                self.assertEqual(names[:2], [".wh.gone", ".wh.swap"])
                self.assertNotIn("keep", names)
                self.assertNotIn("gone/.wh.child", names)
                self.assertEqual(t.extractfile("a").read(), b"new")
                self.assertEqual(t.getmember("b").linkname, "a")
                self.assertEqual(t.getmember("metadata").uid, 1000)
                self.assertEqual(t.getmember("metadata").mode, 0o755)
                self.assertEqual(t.getmember("link").linkname, "/new")
            # Apply this trusted fixture with OCI replacement semantics: regular
            # file replacement unlinks the old inode instead of modifying links.
            root = p / "root"
            root.mkdir()
            with tarfile.open(p / "before") as t:
                t.extractall(root)
            with tarfile.open(p / "delta") as t:
                for member in t:
                    target = root / member.name
                    if target.name.startswith(".wh."):
                        target = target.with_name(target.name[4:])
                        if target.is_dir() and not target.is_symlink():
                            shutil.rmtree(target)
                        else:
                            target.unlink()
                    else:
                        if not member.isdir() and (target.exists() or target.is_symlink()):
                            target.unlink()
                        t.extract(member, root)
            self.assertEqual(set(x.name for x in root.iterdir()), {"keep", "swap", "a", "b", "metadata", "link"})
            self.assertEqual((root / "a").read_bytes(), b"new")
            self.assertEqual((root / "a").stat().st_ino, (root / "b").stat().st_ino)
            self.assertEqual((root / "swap").read_bytes(), b"replacement")
            self.assertEqual(os.readlink(root / "link"), "/new")
            with self.assertRaises(ValueError):
                diff.create_diff(p / "before", p / "after", p / "delta")

    def test_hardlink_order_is_not_a_change(self):
        with tempfile.TemporaryDirectory() as temp:
            p = Path(temp)
            snapshot(p / "before", [("a", tarfile.REGTYPE, b"data", 0o644, 0),
                                    ("b", tarfile.LNKTYPE, "a", 0o644, 0)])
            snapshot(p / "after", [("b", tarfile.REGTYPE, b"data", 0o644, 0),
                                   ("a", tarfile.LNKTYPE, "b", 0o644, 0)])
            self.assertEqual(diff.create_diff(p / "before", p / "after", p / "delta"),
                             {"changed_entries": 0, "whiteouts": 0})

    def test_rejects_bad_snapshot_without_output(self):
        for entries in [
                [("../escape", tarfile.REGTYPE, b"bad", 0o644, 0)],
                [(".wh.reserved", tarfile.REGTYPE, b"", 0o644, 0)],
                [("a", tarfile.LNKTYPE, "b", 0o644, 0), ("b", tarfile.LNKTYPE, "a", 0o644, 0)],
                [("missing/parent", tarfile.REGTYPE, b"bad", 0o644, 0)]]:
            with self.subTest(entries=entries), tempfile.TemporaryDirectory() as temp:
                p = Path(temp)
                snapshot(p / "before", [])
                snapshot(p / "after", entries)
                with self.assertRaises(ValueError):
                    diff.create_diff(p / "before", p / "after", p / "delta")
                self.assertFalse((p / "delta").exists())


if __name__ == "__main__":
    unittest.main()
