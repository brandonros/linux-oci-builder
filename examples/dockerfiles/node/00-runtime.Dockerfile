# Runtime checkpoint for the command-center build stage.
FROM node:22-bookworm-slim
RUN /usr/local/bin/node --version
CMD ["/usr/local/bin/node", "-e", "const fs = require('node:fs'); fs.writeFileSync('/tmp/node-check.txt', 'node-ok'); console.log(fs.readFileSync('/tmp/node-check.txt', 'utf8'))"]
