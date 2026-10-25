#!/usr/bin/env python3
"""Build a complete small Debian image through Rust, verify OCI data, and resume it."""
import argparse
import gzip
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import shutil
import sys
import tarfile
import tempfile
from rust_worker import RustWorker

repo = Path(__file__).resolve().parent.parent
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--worker', type=Path, required=True)
parser.add_argument('--base', type=Path, required=True)
args = parser.parse_args()
worker, base = args.worker.resolve(), args.base.resolve()
assert json.loads((base/'index.json').read_text())['manifests'][0]['digest'] == 'sha256:ec31d744f83f4a73447ba045aaab9409997efe5cc706eca4890c1be36cc1606a'
spec = importlib.util.spec_from_file_location('build_targets', repo/'scripts/build-targets.py')
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)

def blob(layout, descriptor):
    path = layout/'blobs/sha256'/descriptor['digest'].removeprefix('sha256:')
    assert path.stat().st_size == descriptor['size']
    assert 'sha256:'+hashlib.sha256(path.read_bytes()).hexdigest() == descriptor['digest']
    return path

with tempfile.TemporaryDirectory(prefix='rust-full-build-') as temporary:
    directory = Path(temporary)
    pinned_worker = directory/'blink-worker'
    shutil.copy2(worker, pinned_worker)
    worker = pinned_worker
    context, output = directory/'context', directory/'build'
    context.mkdir()
    (context/'payload.txt').write_text('rust builder works\n')
    (context/'Dockerfile').write_text('''FROM ${BASE_IMAGE}:${BASE_TAG}
WORKDIR /work
COPY payload.txt /work/input.txt
RUN tr 'a-z' 'A-Z' < input.txt > output.txt && mkdir -p /out && cp output.txt /out/result.txt
RUN chown 123:456 /out/result.txt && chmod 400 /out/result.txt
CMD ["/bin/cat", "/out/result.txt"]
''')
    command = [sys.executable, str(repo/'scripts/build-targets.py'), '--runtime-kind', 'rust', '--blink', str(worker), '--rootless-metadata', '--no-jit', '--base', str(base), '--context', str(context), '--dockerfile', str(context/'Dockerfile'), '--output', str(output), '--timeout', '180']
    subprocess.run(command, cwd=repo, check=True, timeout=600)
    complete = json.loads((output/'complete.json').read_text())
    image = Path(complete['image'])
    index = json.loads((image/'index.json').read_text())
    manifest = json.loads(blob(image, index['manifests'][0]).read_text())
    config = json.loads(blob(image, manifest['config']).read_text())
    assert (config['os'], config['architecture']) == ('linux', 'amd64')
    assert config['config']['Cmd'] == ['/bin/cat', '/out/result.txt']
    assert config['config']['WorkingDir'] == '/work'
    assert len(manifest['layers']) == len(config['rootfs']['diff_ids']) == 2
    rebuilt = directory/'rebuilt'
    rebuilt.mkdir()
    owners = {}
    for layer, diff_id in zip(manifest['layers'], config['rootfs']['diff_ids']):
        path = blob(image, layer)
        opener = gzip.open if layer['mediaType'].endswith('gzip') else open
        digest = hashlib.sha256()
        with opener(path, 'rb') as stream:
            for chunk in iter(lambda: stream.read(1024*1024), b''):
                digest.update(chunk)
        assert diff_id == 'sha256:'+digest.hexdigest()
        builder.apply_layer(path, rebuilt, owners)
    with tarfile.open(blob(image, manifest['layers'][-1])) as archive:
        members = {member.name.removeprefix('./'): member for member in archive}
        result = members['out/result.txt']
        assert (result.uid, result.gid, result.mode) == (123, 456, 0o400)
        assert archive.extractfile(result).read() == b'RUST BUILDER WORKS\n'
        assert not any(name.startswith('.image-forge') or name.startswith('SystemRoot') for name in members)
    assert (rebuilt/'out/result.txt').read_bytes() == b'RUST BUILDER WORKS\n'
    # Archive headers verify metadata; executing a separately extracted root
    # verifies image content/loader resolution. This is not metadata import.
    runtime = RustWorker(worker, rebuilt, directory/'verify-runtime')
    try:
        runtime.checkpoint(directory/'verify', config['config']['Cmd'], 0, 0, '/root', '/work', {'PATH':'/usr/bin:/bin'}, 30)
    finally:
        runtime.close()
    assert (directory/'verify/stdout').read_bytes() == b'RUST BUILDER WORKS\n'
    receipts = {path.relative_to(output).as_posix(): path.read_bytes() for path in output.glob('*/result.json')}
    assert len(receipts) == 7
    for value in receipts.values():
        record = json.loads(value)
        assert record['returncode'] == 0 and record['cleanup_verified']
        assert record['runtime_kind'] == 'rust-worker-v1'
    resumed = subprocess.run(command, cwd=repo, capture_output=True, timeout=120, text=True)
    assert resumed.returncode == 0, resumed.stdout + resumed.stderr
    assert resumed.stdout.count('RESUME ') == 7 and 'RUN ' not in resumed.stdout
    assert receipts == {path.relative_to(output).as_posix(): path.read_bytes() for path in output.glob('*/result.json')}
    report = dict(passed=True, manifest=index['manifests'][0]['digest'], layers=len(manifest['layers']), checkpoints=len(receipts), owner=[result.uid,result.gid], mode=oct(result.mode), resumed_without_reexecution=True)
    (repo/'target').mkdir(exist_ok=True)
    (repo/'target/rust-build-results.json').write_text(json.dumps(report, indent=2)+'\n')
    print(json.dumps(report), flush=True)
