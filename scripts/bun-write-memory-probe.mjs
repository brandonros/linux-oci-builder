const bytes=new Uint8Array(16*1024*1024);
const t=Date.now();
const written=await Bun.write('/tmp/bun-memory-write.bin',bytes);
console.log('BUN_MEMORY_WRITE',JSON.stringify({bytes:written,ms:Date.now()-t}));
process.exit(0);
