#!/usr/bin/env python3
"""Real Rust stage-worker integration, receipts, recovery and root locking."""
import argparse
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
from types import SimpleNamespace
from rust_worker import RustWorker

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--worker', type=Path, required=True)
parser.add_argument('--root', type=Path, required=True)
args = parser.parse_args()
spec = importlib.util.spec_from_file_location('build_targets', Path(__file__).with_name('build-targets.py'))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
with tempfile.TemporaryDirectory(prefix='rust-stage-') as directory:
    output = Path(directory)/'build'
    output.mkdir()
    root = output/'rootfs'
    shutil.copytree(args.root, root, symlinks=True)
    build = module.Build.__new__(module.Build)
    build.output, build.root, build.step, build.rust_worker = output, root, 0, None
    build.args = SimpleNamespace(blink=args.worker.resolve(), previous_blink=None, timeout=10,
        no_jit=True, rootless_metadata=True, fault_trace=False, runtime_kind='rust')
    seed = ['/bin/sh','-c','printf persisted > /tmp/stage; chown 123:456 /tmp/stage; chmod 000 /tmp/stage']
    verify = ['/bin/sh','-c',"cat /tmp/stage; stat -c '%u:%g:%a' /tmp/stage"]
    try:
        build.run('seed', seed)
        pid = build.rust_worker.process.pid
        build.run('verify', verify)
        assert build.rust_worker.process.pid == pid
        assert (output/'002-verify/stdout').read_bytes() == b'persisted123:456:0\n'
        assert json.loads((output/'002-verify/result.json').read_text())['cleanup_verified']
        assert json.loads((output/'002-verify/result.json').read_text())['env']['RES_OPTIONS'] == 'use-vc'
        try:
            other = RustWorker(args.worker, root, output/'competing')
        except BlockingIOError:
            pass
        else:
            other.close()
            raise AssertionError('concurrent rootfs mutation lock was not enforced')
        try:
            build.run('failed', ['/bin/sh','-c','exit 7'])
        except subprocess.CalledProcessError as error:
            assert error.returncode == 7
        else:
            raise AssertionError('nonzero exit accepted')
        assert not (output/'003-failed/build-step.json').exists()
        build.args.timeout = .02
        try:
            build.run('timeout', ['/bin/sh','-c','sleep 20'])
        except subprocess.CalledProcessError as error:
            assert error.returncode == 124
        else:
            raise AssertionError('timeout accepted')
        assert not (output/'004-timeout/build-step.json').exists()
        build.args.timeout = 10
        build.run('recovery', ['/bin/sh','-c','printf recovered'])
        assert build.rust_worker.process.pid == pid
        assert (output/'005-recovery/stdout').read_bytes() == b'recovered'
    finally:
        build.close()
    build.step = 0
    build.run('seed', seed)
    build.run('verify', verify)
    assert build.rust_worker is None, 'resuming completed receipts started a worker'
    standalone = Path(directory)/'standalone'
    subprocess.run(['python3', str(Path(__file__).with_name('blink-checkpoint.py')),
        '--blink', str(args.worker.resolve()), '--runtime-kind', 'rust', '--rootless-metadata',
        '--rootfs', str(root), '--output', str(standalone), '--dns-tcp', '--uid', '17', '--gid', '19',
        '--', '/bin/sh', '-c', 'id -u; id -g; printf \"%s\\n\" \"$RES_OPTIONS\"'], check=True)
    assert (standalone/'stdout').read_bytes() == b'17\n19\nuse-vc\n'
print('Rust stage integration: PASS (reuse, metadata, failure/timeout recovery, receipts, locking, standalone checkpoint)')
