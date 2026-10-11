const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

function capture() {
  const packets = [];
  let Processor;
  vm.runInNewContext(fs.readFileSync(path.resolve(__dirname, '../../app/static/onair/microphone-worklet.js'), 'utf8'), {
    AudioWorkletProcessor: class { constructor() { this.port = { postMessage: (bytes) => packets.push(new Int16Array(bytes)) }; } },
    registerProcessor: (name, type) => { assert.equal(name, 'radiotedu-microphone'); Processor = type; },
  });
  return { node: new Processor(), packets };
}

test('worklet preserves packet continuity across 128-frame render blocks', () => {
  const { node, packets } = capture();
  node.port.onmessage({ data: { enabled: true } });
  for (let i = 0; i < 75; i++) node.process([[new Float32Array(128).fill(0.5)]]);
  assert.equal(packets.length, 2);
  assert.equal(packets[0].length, 4800);
  assert.ok(packets.every(p => [...p].every(v => v === 16384)));
  assert.equal(node.offset, 0);
});

test('disarm discards partial audio and sends no microphone samples', () => {
  const { node, packets } = capture();
  node.port.onmessage({ data: { enabled: true } });
  node.process([[new Float32Array(128).fill(1)]]);
  node.port.onmessage({ data: { enabled: false } });
  for (let i = 0; i < 40; i++) node.process([[new Float32Array(128).fill(-1)]]);
  assert.equal(packets.length, 0);
  assert.equal(node.offset, 0);
});
