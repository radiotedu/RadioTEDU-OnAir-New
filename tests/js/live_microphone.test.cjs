const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');
const source = fs.readFileSync(path.resolve(__dirname, '../../app/static/onair/live-microphone.js'), 'utf8');

function rig() {
  const calls = [];
  const tracks = [{ readyState: 'live', stop() { this.readyState = 'ended'; }, getSettings() { return { deviceId: 'studio-usb' }; }, addEventListener() {} }];
  const stream = { getTracks: () => tracks, getAudioTracks: () => tracks };
  const node = { port: { postMessage(message) { if (message.enabled) queueMicrotask(() => this.onmessage?.({ data: new ArrayBuffer(9600) })); } }, connect() { return this; }, disconnect() {} };
  const context = {
    console, setTimeout, clearTimeout, module: { exports: {} },
    navigator: { mediaDevices: { getUserMedia: async (constraints) => { calls.push(['capture', constraints]); return stream; }, enumerateDevices: async () => [{ kind: 'audioinput', deviceId: 'studio-usb', label: 'USB Studio' }] } },
    AudioContext: class { constructor() { this.sampleRate = 48000; this.audioWorklet = { addModule: async () => {} }; } createMediaStreamSource() { return node; } createGain() { return { ...node, gain: {} }; } resume() { return Promise.resolve(); } close() { return Promise.resolve(); } },
    AudioWorkletNode: class { constructor() { return node; } },
  };
  context.window = context;
  vm.runInNewContext(source, context);
  let station = 4;
  let saved = { program_music_mode: 'normal', mic_gain: 1, music_gain: 1, duck_level: 0.15 };
  const options = {
    station: () => station,
    studio: async () => { calls.push(['studio']); },
    api: async (url, init = {}) => {
      calls.push([url, init]);
      if (url.includes('/runtime/')) return { output_feed_active: true, program_running: true };
      if (url.includes('/live/settings')) saved = JSON.parse(init.body);
      if (url.includes('/render/chunk')) return { receiving: true };
      return { ...saved };
    },
  };
  return { context, stream, tracks, node, calls, options, mic: new context.RadioLiveMicrophone(options), station: (id) => { station = id; } };
}

test('selected USB device uses raw 48 kHz without browser gain/noise processing', async () => {
  const r = rig();
  await r.mic.selectDevice('studio-usb');
  await r.mic.prepare();
  const audio = r.calls.find(c => c[0] === 'capture')[1].audio;
  assert.equal(audio.deviceId.exact, 'studio-usb');
  assert.equal(audio.sampleRate, 48000);
  assert.equal(audio.autoGainControl, false);
  assert.equal(audio.noiseSuppression, false);
  assert.equal(r.calls.filter(c => c[0].includes('/render/')).length, 0);
});

test('release during a permission prompt cannot put the microphone on air later', async () => {
  const r = rig();
  let grant;
  r.context.navigator.mediaDevices.getUserMedia = () => new Promise(resolve => { grant = resolve; });
  const starting = r.mic.start().catch(e => e.message);
  const stopping = r.mic.stop();
  grant(r.stream);
  await Promise.all([starting, stopping]);
  assert.equal(r.mic.phase, 'off');
  assert.equal(r.tracks[0].readyState, 'ended');
  assert.equal(r.calls.filter(c => c[0].includes('/render/start')).length, 0);
});

test('rapid clicks start only one session and stop restores the previous mix', async () => {
  const r = rig();
  await Promise.all([r.mic.start(), r.mic.start()]);
  assert.equal(r.mic.phase, 'live');
  assert.equal(r.calls.filter(c => c[0].includes('/render/start')).length, 1);
  await r.mic.stop();
  assert.equal(r.mic.phase, 'off');
  assert.equal(r.calls.filter(c => c[0].includes('/render/stop')).length, 1);
  assert.equal(r.mic.original, null);
});

test('stop remains bound to the original station after selection changes', async () => {
  const r = rig();
  await r.mic.start();
  r.station(9);
  await r.mic.stop();
  const stop = r.calls.find(c => c[0].includes('/render/stop'));
  assert.equal(JSON.parse(stop[1].body).station_id, 4);
});

test('an occupied live input cannot be replaced by a microphone click', async () => {
  const r = rig();
  r.options.api = async (url) => url.includes('/runtime/') ? { output_feed_active: true, program_running: true, live_mic_active: true } : {};
  await assert.rejects(r.mic.start(), /already owns/);
  assert.equal(r.mic.phase, 'off');
  assert.equal(r.calls.some(c => c[0] === 'studio'), false);
});

test('mix settings are rejected when persisted read-back differs', async () => {
  const r = rig();
  r.options.api = async () => ({ program_music_mode: 'mute', mic_gain: 1, music_gain: 1, duck_level: 0.15 });
  await assert.rejects(r.mic.saveSettings(), /did not persist/);
});

test('an unaccepted source cannot be presented as a live microphone', async () => {
  const r = rig();
  r.options.api = async () => ({ output_feed_active: true, program_running: true, icecast_mount_health: { source_response_confirmed: false } });
  await assert.rejects(r.mic.start(), /not been accepted/);
  assert.equal(r.mic.phase, 'off');
  assert.equal(r.mic.renderStarted, false);
});

test('transport failure while confirming input releases the source and restores music', async () => {
  const r = rig();
  const api = r.options.api;
  r.options.api = async (url, init) => {
    if (url.includes('/render/chunk')) throw new Error('connection lost');
    return api(url, init);
  };
  await assert.rejects(r.mic.start(), /connection lost/);
  if (r.mic.stopPromise) await r.mic.stopPromise;
  assert.equal(r.mic.phase, 'off');
  assert.equal(r.mic.renderStarted, false);
  assert.equal(r.mic.original, null);
});

test('failed restoration remains visible and a second stop retries it', async () => {
  const r = rig();
  await r.mic.start();
  const api = r.options.api;
  let fail = true;
  r.options.api = async (url, init) => {
    if (url.includes('/live/settings') && fail) throw new Error('offline');
    return api(url, init);
  };
  await assert.rejects(r.mic.stop(), /offline/);
  assert.equal(r.mic.phase, 'error');
  assert.ok(r.mic.original);
  fail = false;
  await r.mic.stop();
  assert.equal(r.mic.phase, 'off');
  assert.equal(r.mic.original, null);
});
