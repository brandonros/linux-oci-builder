const values = new Uint32Array(9);
values.fill(0xffffffff);
for (let i = 0; i < 10000; i++) {
  const object = { a: values[0], b: values[1], c: values[2], d: values[3],
    e: values[4], f: values[5], g: values[6], h: values[7], j: values[8] };
  for (const [field, value] of Object.entries(object)) {
    if (value !== 4294967295) {
      console.error(JSON.stringify({ i, field, value, object }));
      process.exit(1);
    }
  }
}
console.log('Uint32 reads and property stores passed');
