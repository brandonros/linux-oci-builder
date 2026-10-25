let remaining=1024;
const chunk=new Uint8Array(16384);
const body=new ReadableStream({pull(controller){if(remaining-->0)controller.enqueue(chunk);else controller.close();}});
const t=Date.now();
const bytes=await Bun.write('/tmp/bun-synthetic-stream.bin',new Response(body));
console.log('BUN_SYNTHETIC_STREAM',JSON.stringify({bytes,ms:Date.now()-t}));
process.exit(0);
