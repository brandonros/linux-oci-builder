const { createRequire } = require('node:module');
const request = createRequire('/usr/lib/code-server/out/node/entry.js');
for (let i = 0; i < 100; i++) {
  const result = request.resolve('body-parser');
  if (result !== '/usr/lib/code-server/node_modules/body-parser/index.js') throw new Error(result);
}
console.log('body-parser resolution passed');
request('body-parser');
console.log('body-parser import passed');
