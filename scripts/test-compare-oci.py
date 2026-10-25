#!/usr/bin/env python3
import importlib.util
import io
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('compare', Path(__file__).with_name('compare-oci.py'))
compare = importlib.util.module_from_spec(spec)
spec.loader.exec_module(compare)


class InventoryTest(unittest.TestCase):
    def test_full_metadata_keeps_old_inode_after_target_replacement(self):
        with tempfile.TemporaryDirectory() as tmp:
            layers = []
            for number in range(3):
                path = Path(tmp) / f'{number}.tar'
                with tarfile.open(path, 'w', format=tarfile.PAX_FORMAT) as archive:
                    if number != 1:
                        info = tarfile.TarInfo('original')
                        info.size = 3
                        info.pax_headers = {'mtime': '123.123456789'}
                        archive.addfile(info, io.BytesIO(b'old' if number == 0 else b'new'))
                    else:
                        for name in ('alias1', 'alias2'):
                            info = tarfile.TarInfo(name)
                            info.type, info.linkname = tarfile.LNKTYPE, 'original'
                            archive.addfile(info)
                layers.append(path)
            with patch.object(compare.oci, 'verify', return_value=('digest', {}, layers)):
                _, _, entries = compare.inventory(Path(tmp), full_metadata=True)
            self.assertEqual(entries['original']['hardlinks'], [])
            self.assertEqual(entries['alias1']['hardlinks'], ['alias1', 'alias2'])
            self.assertEqual(entries['alias1']['mtime'], '123.123456789')
            self.assertIsNone(entries['alias1']['atime'])
            self.assertEqual(entries['alias1']['sha256'], compare.hashlib.sha256(b'old').hexdigest())
            self.assertEqual(entries['original']['sha256'], compare.hashlib.sha256(b'new').hexdigest())

    def test_whiteouts_and_hardlinks(self):
        with tempfile.TemporaryDirectory() as tmp:
            layers = []
            for number, rows in enumerate([
                    [('gone', b'old'), ('dir/old', b'old'), ('keep', b'same')],
                    [('gone', b'new'), ('.wh.gone', b''), ('dir/new', b'new'),
                     ('dir/.wh..wh..opq', b''), ('link', None)]]):
                path = Path(tmp) / f'{number}.tar'
                with tarfile.open(path, 'w') as archive:
                    for name, data in rows:
                        info = tarfile.TarInfo(name)
                        if data is None:
                            info.type, info.linkname = tarfile.LNKTYPE, 'keep'
                            archive.addfile(info)
                        else:
                            info.size = len(data)
                            archive.addfile(info, io.BytesIO(data))
                layers.append(path)
            with patch.object(compare.oci, 'verify', return_value=('digest', {}, layers)):
                _, _, entries = compare.inventory(Path(tmp))
            self.assertEqual(set(entries), {'gone', 'dir/new', 'keep', 'link'})
            self.assertEqual(entries['keep'], entries['link'])
            self.assertEqual(entries['gone']['sha256'], compare.hashlib.sha256(b'new').hexdigest())


if __name__ == '__main__':
    unittest.main()
