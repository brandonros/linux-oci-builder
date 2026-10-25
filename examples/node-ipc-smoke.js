// Run with the guest Node binary. Exercises code-server's fork + IPC pattern.
const { fork } = require('node:child_process');
const started = Date.now();
const log = (message) => console.log(`${Date.now() - started}ms pid=${process.pid} ${message}`);
if (process.argv[2] === 'child') {
  log('child started');
  process.on('message', (message) => {
    if (message.type !== 'reply') process.exit(2);
    log('child received reply');
    process.disconnect();
  });
  process.send({ type: 'handshake' });
} else {
  const child = fork(__filename, ['child'], { stdio: ['pipe', 'pipe', 'pipe', 'ipc'] });
  log(`spawned ${child.pid}`);
  child.stdout.pipe(process.stdout);
  child.stderr.pipe(process.stderr);
  const timeout = setTimeout(() => {
    log('IPC timed out');
    child.kill('SIGKILL');
    process.exitCode = 1;
  }, 30000);
  child.on('message', (message) => {
    log(`parent received ${JSON.stringify(message)}`);
    if (message.type !== 'handshake') process.exitCode = 2;
    child.send({ type: 'reply' });
  });
  child.on('error', (error) => { console.error(error); process.exitCode = 3; });
  child.on('exit', (code, signal) => {
    clearTimeout(timeout);
    log(`child exit=${code} signal=${signal}`);
    if (code !== 0 || signal) process.exitCode = 4;
  });
}
