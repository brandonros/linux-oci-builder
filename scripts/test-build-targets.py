import importlib.util
import io
from pathlib import Path
import tarfile
import tempfile
import unittest
import json
from types import SimpleNamespace
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("build_targets", Path(__file__).with_name("build-targets.py"))
build = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build)


def archive(path, entries):
    with tarfile.open(path, "w") as tar:
        for name, kind, value in entries:
            member = tarfile.TarInfo(name)
            member.type, member.uid, member.gid = kind, 1000, 1000
            if kind == tarfile.SYMTYPE:
                member.linkname = value
            elif kind == tarfile.REGTYPE:
                member.size = len(value)
            tar.addfile(member, io.BytesIO(value) if kind == tarfile.REGTYPE else None)


class RuntimeConfigTests(unittest.TestCase):
    def test_completed_checkpoint_survives_diagnostic_mode_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            instance = build.Build.__new__(build.Build)
            instance.output, instance.root, instance.step = root, root / "rootfs", 0
            instance.args = SimpleNamespace(blink=root / "blink", previous_blink=None,
                timeout=60, no_jit=False, rootless_metadata=True, fault_trace=False)
            def checkpoint(command, **kwargs):
                evidence = Path(command[command.index("--output") + 1])
                evidence.mkdir()
                (evidence / "result.json").write_text(json.dumps({"cleanup_verified": True}))
            with patch.object(build.subprocess, "run", side_effect=checkpoint) as run:
                instance.run("run", ["/bin/true"])
                instance.step = 0
                instance.args.no_jit, instance.args.fault_trace = True, True
                instance.run("run", ["/bin/true"])
                self.assertEqual(run.call_count, 1)

    def test_entrypoint_resets_only_inherited_cmd(self):
        for recipe, expected in [
            ([("ENTRYPOINT", '["server"]')], None),
            ([("CMD", '["--port", "8080"]'), ("ENTRYPOINT", '["server"]')], ["--port", "8080"]),
            ([("ENTRYPOINT", '["server"]'), ("CMD", '["--port", "8080"]')], ["--port", "8080"]),
        ]:
            with self.subTest(recipe=recipe):
                instance = build.Build.__new__(build.Build)
                instance.config = {"Cmd": ["bash"]}
                instance.recipe = recipe
                instance.execute()
                self.assertIn("Cmd", instance.config)
                self.assertEqual(instance.config["Cmd"], expected)


class LayerTests(unittest.TestCase):
    def test_whiteouts_and_opaque_directory_preserve_new_layer_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp); root = p / "root"; root.mkdir()
            (root / "d").mkdir(); (root / "d/old").write_text("old")
            (root / "gone").write_text("gone")
            owners = {"d": [0, 0], "d/old": [5, 5], "gone": [5, 5]}
            archive(p / "layer", [("d/new", tarfile.REGTYPE, b"new"),
                                   ("d/.wh..wh..opq", tarfile.REGTYPE, b""),
                                   (".wh.gone", tarfile.REGTYPE, b"")])
            build.apply_layer(p / "layer", root, owners)
            self.assertEqual((root / "d/new").read_bytes(), b"new")
            self.assertFalse((root / "d/old").exists())
            self.assertFalse((root / "gone").exists())
            self.assertEqual(owners, {"d": [0, 0], "d/new": [1000, 1000]})

    def test_absolute_guest_symlink_does_not_delete_host_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp); root = p / "root"; root.mkdir()
            (root / "inside").mkdir(); (root / "inside/file").write_text("guest")
            (root / "alias").symlink_to("/inside")
            archive(p / "layer", [("alias/.wh.file", tarfile.REGTYPE, b"")])
            build.apply_layer(p / "layer", root, {})
            self.assertFalse((root / "inside/file").exists())
            self.assertTrue((root / "alias").is_symlink())

    def test_unsafe_member_is_rejected_before_mutating_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp); root = p / "root"; root.mkdir()
            archive(p / "layer", [("good", tarfile.REGTYPE, b"good"),
                                   ("../escape", tarfile.REGTYPE, b"bad")])
            with self.assertRaises(ValueError):
                build.apply_layer(p / "layer", root, {})
            self.assertEqual(list(root.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
