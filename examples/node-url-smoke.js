// Reduced from Node's package-exports containment check.
const base = new URL('file:///usr/lib/code-server/node_modules/body-parser/package.json');
for (let i = 0; i < 100; i++) {
  const resolved = new URL('./index.js', base).pathname;
  const directory = new URL('.', base).pathname;
  if (resolved !== '/usr/lib/code-server/node_modules/body-parser/index.js' ||
      directory !== '/usr/lib/code-server/node_modules/body-parser/' ||
      !resolved.startsWith(directory)) {
    console.error(JSON.stringify({ i, resolved, directory, prefix: resolved.startsWith(directory) }));
    process.exit(1);
  }
}
console.log('URL resolution and prefix check passed');
