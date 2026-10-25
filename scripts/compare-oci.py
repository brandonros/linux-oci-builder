#!/usr/bin/env python3
"""Compare verified OCI filesystem contents without executing either image."""
import argparse
import collections
from decimal import Decimal
import hashlib
import importlib.util
import json
from pathlib import Path, PurePosixPath
import tarfile

spec = importlib.util.spec_from_file_location('verify_oci', Path(__file__).with_name('run-image.py'))
oci = importlib.util.module_from_spec(spec)
spec.loader.exec_module(oci)


def inventory(layout, full_metadata=False):
    digest, config, layers = oci.verify(layout)
    entries = {}
    inode = 0
    for layer in layers:
        additions, deletions, opaque = {}, [], []
        with tarfile.open(layer, mode='r|*') as archive:
            for member in archive:
                name = str(PurePosixPath(member.name)).lstrip('/')
                path = PurePosixPath(name)
                if path.name == '.wh..wh..opq':
                    opaque.append(str(path.parent))
                    continue
                if path.name.startswith('.wh.'):
                    deletions.append(str(path.parent / path.name[4:]))
                    continue
                row = dict(type=member.type.decode(), uid=member.uid, gid=member.gid,
                           mode=member.mode, link=member.linkname,
                           xattrs={k: v for k, v in member.pax_headers.items() if k.startswith('SCHILY.xattr.')})
                if full_metadata:
                    row['mtime'] = str(Decimal(str(member.pax_headers.get('mtime', member.mtime))).normalize())
                    for key in ('atime', 'ctime'):
                        row[key] = (str(Decimal(member.pax_headers[key]).normalize())
                                    if key in member.pax_headers else None)
                    row['extra_pax'] = {k: v for k, v in member.pax_headers.items()
                                        if k not in ('mtime', 'atime', 'ctime', 'path', 'linkpath', 'size', 'uid', 'gid', 'uname', 'gname')
                                        and not k.startswith('SCHILY.xattr.')}
                if member.isfile():
                    inode += 1
                    if full_metadata:
                        row['_inode'] = inode
                    h = hashlib.sha256()
                    with archive.extractfile(member) as stream:
                        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                            h.update(chunk)
                    row.update(size=member.size, sha256=h.hexdigest())
                if member.ischr() or member.isblk():
                    row.update(devmajor=member.devmajor, devminor=member.devminor)
                if member.islnk():
                    target = str(PurePosixPath(member.linkname)).lstrip('/')
                    source = additions.get(target, entries.get(target))
                    if source is None or 'sha256' not in source:
                        raise ValueError('Unresolved hardlink: ' + name + ' -> ' + target)
                    if full_metadata:
                        # A hardlink refers to the target inode, including its metadata.
                        row = dict(source)
                    else:
                        row.update(type='0', link='', size=source['size'], sha256=source['sha256'])
                additions[name] = row
        for name in list(entries):
            if any(name == d or name.startswith(d + '/') for d in deletions) or any(
                    (name != '.' if d == '.' else name.startswith(d + '/')) for d in opaque):
                del entries[name]
        for name, row in additions.items():
            if row['type'] != '5':
                for child in [p for p in entries if p.startswith(name + '/')]:
                    del entries[child]
            entries[name] = row
    if full_metadata:
        groups = collections.defaultdict(list)
        for name, row in entries.items():
            if '_inode' in row:
                groups[row['_inode']].append(name)
        for row in entries.values():
            if '_inode' in row:
                group = sorted(groups[row.pop('_inode')])
                row['hardlinks'] = group if len(group) > 1 else []
    return digest, config, entries


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--local', type=Path, required=True)
    parser.add_argument('--published', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--full-metadata', action='store_true', help='Include archived timestamps, hardlink topology, and extra PAX metadata')
    args = parser.parse_args()
    sides = []
    for path in (args.local, args.published):
        print('Hashing ' + str(path), flush=True)
        sides.append(inventory(path, args.full_metadata))
    (ld, lc, le), (pd, pc, pe) = sides
    changed = []
    for name in sorted(le.keys() & pe.keys()):
        fields = {k: {'local': le[name].get(k), 'published': pe[name].get(k)}
                  for k in le[name].keys() | pe[name].keys() if le[name].get(k) != pe[name].get(k)}
        if fields:
            changed.append({'path': name, 'fields': fields})
    field_counts = collections.Counter(k for row in changed for k in row['fields'])
    config_diff = {k: {'local': lc.get('config', {}).get(k), 'published': pc.get('config', {}).get(k)}
                   for k in lc.get('config', {}).keys() | pc.get('config', {}).keys()
                   if lc.get('config', {}).get(k) != pc.get('config', {}).get(k)}
    result = dict(local={'path': str(args.local), 'manifest': ld, 'entries': len(le), 'config': lc},
                  published={'path': str(args.published), 'manifest': pd, 'entries': len(pe), 'config': pc},
                  config_differences=config_diff,
                  image_metadata_differences={k: {'local': lc.get(k), 'published': pc.get(k)}
                                              for k in lc.keys() | pc.keys()
                                              if k != 'config' and lc.get(k) != pc.get(k)},
                  changed=changed,
                  only_local=sorted(le.keys() - pe.keys()), only_published=sorted(pe.keys() - le.keys()),
                  summary={'changed': len(changed), 'only_local': len(le.keys() - pe.keys()),
                           'only_published': len(pe.keys() - le.keys()), 'fields': dict(field_counts)},
                  comparison_notes=['All regular file bytes hashed; hardlinks normalized to file contents.',
                                    'Timestamps, layer boundaries, tar headers and build history excluded from filesystem equality.'])
    if args.full_metadata:
        result['comparison_notes'] = [
            'Layers replayed with whiteouts; all regular file bytes hashed without host extraction.',
            'Includes mtime, recorded atime/ctime (null means absent), final hardlink groups, and extra PAX fields.',
            'Hardlink headers refer to target inode metadata; inode numbers and tar ordering are not filesystem content.',
            'UID/GID are authoritative; tar user/group labels are not separately compared.',
            'This compares archived metadata, not host-generated extraction timestamps or runtime-mounted files.']
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + '\n')
    print(json.dumps(result['summary'], indent=2), flush=True)


if __name__ == '__main__':
    main()
