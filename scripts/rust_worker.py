"""Persistent Rust Blink context for one trusted build stage (no C fallback)."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import selectors
import shutil
import subprocess
import time


class RustWorker:
    def __init__(self, binary, rootfs, output):
        self.root = Path(rootfs).resolve(strict=True)
        self.output = Path(output).resolve()
        self.output.mkdir(parents=True, exist_ok=True)
        self.lock = (self.root.parent / (self.root.name + '.checkpoint.lock')).open('a')
        self.process = None
        self.log = None
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            # Pin the executed artifact for the lifetime of this stage context.
            runtime = self.output / 'rust-worker'
            shutil.copy2(Path(binary).resolve(strict=True), runtime)
            self.digest = hashlib.sha256(runtime.read_bytes()).hexdigest()
            self.log = (self.output / 'rust-worker.stderr').open('ab')
            self.process = subprocess.Popen([str(runtime)], stdin=subprocess.PIPE,
                                            stdout=subprocess.PIPE, stderr=self.log,
                                            pass_fds=(self.lock.fileno(),))
            self._request(dict(op='create', context='stage', rootfs=str(self.root),
                               rootless_metadata=True, network=True), 10)
        except BaseException:
            self.abort()
            raise

    def _request(self, message, timeout):
        payload = (json.dumps(message)+'\n').encode()
        if len(payload) > 1024*1024:
            raise ValueError('Rust worker request exceeds 1 MiB')
        if self.process.poll() is not None:
            raise RuntimeError('Rust worker has exited; inspect rust-worker.stderr')
        self.process.stdin.write(payload)
        self.process.stdin.flush()
        deadline = time.monotonic()+timeout
        reply = bytearray()
        with selectors.DefaultSelector() as selector:
            selector.register(self.process.stdout, selectors.EVENT_READ)
            while b'\n' not in reply:
                remaining = deadline-time.monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    self.abort()
                    raise TimeoutError('Rust worker protocol timeout; stage context terminated')
                chunk = os.read(self.process.stdout.fileno(), 65536)
                if not chunk:
                    raise RuntimeError('Rust worker protocol ended; inspect rust-worker.stderr')
                reply.extend(chunk)
                if len(reply) > 1024*1024:
                    self.abort()
                    raise ValueError('Rust worker reply exceeds 1 MiB')
        result = json.loads(reply)
        if not result.get('ok'):
            raise RuntimeError('Rust runtime: '+str(result.get('error')))
        return result

    def checkpoint(self, output, argv, uid=0, gid=0, home='/root', workdir='/', env=None, timeout=900):
        output = Path(output).resolve()
        if output == self.root or self.root in output.parents:
            raise ValueError('checkpoint output must be outside rootfs')
        if timeout <= 0:
            raise ValueError('positive timeout required')
        output.mkdir(parents=True, exist_ok=False)
        guest_env = {'HOME': home, 'PATH': '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin', **(env or {})}
        manifest = dict(rootfs=str(self.root), argv=argv, uid=uid, gid=gid, workdir=workdir,
                        env=guest_env, mode='rust-interpreter', runtime_kind='rust-worker-v1',
                        metadata_backend='blink-rootless', blink_sha256=self.digest,
                        started=time.time(), timeout_seconds=timeout)
        report = output/'result.json'
        report.write_text(json.dumps(manifest, indent=2)+'\n')
        try:
            result = self._request(dict(op='run', context='stage', argv=argv, cwd=workdir,
                env=guest_env, uid=uid, gid=gid, timeout_ms=max(1, int(timeout*1000)),
                stdout=str(output/'stdout'), stderr=str(output/'stderr')), timeout+10)
            manifest.update(result)
            # run_with_io returns only after its owned guest process tree is dropped.
            manifest['cleanup_verified'] = True
            manifest['returncode'] = (result['code'] if result['termination']=='exited'
                else 128+result['code'] if result['termination']=='signaled'
                else 124 if result['termination']=='timed_out' else 125)
        except BaseException as error:
            manifest['runtime_error'] = str(error)
            raise
        finally:
            manifest['finished'] = time.time()
            report.write_text(json.dumps(manifest, indent=2)+'\n')
        if manifest['returncode'] != 0:
            raise subprocess.CalledProcessError(manifest['returncode'], argv)
        return manifest

    def close(self):
        try:
            if self.process is not None:
                self._request(dict(op='close', context='stage'), 10)
                self.process.stdin.close()
                code = self.process.wait(timeout=10)
                if code:
                    raise RuntimeError(f'Rust worker exited {code}')
        finally:
            self.abort()

    def abort(self):
        if self.process is not None:
            if self.process.poll() is None:
                self.process.kill()
                self.process.wait()
            for stream in (self.process.stdin, self.process.stdout):
                try:
                    stream.close()
                except OSError:
                    pass
            self.process = None
        if self.log is not None:
            self.log.close()
            self.log = None
        self.lock.close()
