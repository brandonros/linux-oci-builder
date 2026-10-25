const t = Date.now();
const response = await fetch('https://downloads.claude.ai/claude-code-releases/2.1.310/linux-x64/claude', {headers:{Range:'bytes=0-16777215'}});
if(response.status !== 206) throw new Error('Unexpected status '+response.status);
console.log('HEADERS',Date.now()-t);
let bytes;
if(process.env.PROBE_MODE==='file') bytes=await Bun.write('/tmp/bun-transfer-probe.bin',response);
else bytes=(await response.arrayBuffer()).byteLength;
console.log('BUN_TRANSFER_PROBE',JSON.stringify({mode:process.env.PROBE_MODE,bytes,ms:Date.now()-t}));
process.exit(0);
