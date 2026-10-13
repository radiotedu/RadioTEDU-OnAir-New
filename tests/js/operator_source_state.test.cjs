const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const { appJs, section } = require('./unified_test_helpers.cjs');

function rig({ runtime = {}, health = {}, queue = [], programmes = [], publicStation = { status: 'stopped' } } = {}) {
  const state = { runtime, health, queue, operatorProgrammes: programmes, publicStation };
  const context = { state, URL, Date, Map, console };
  const functions = section(appJs, 'function operatorSourceSnapshot(', 'function renderCoreStatus(');
  vm.runInNewContext(functions, context);
  return { ...context, state };
}

function sending() {
  return { running: true, output_feed_active: true, program_running: true,
    required_outputs: { icecast: true, local: false }, source_health: { icecast: true },
    delivery_health: { icecast: false }, worker_loop: { running: true } };
}

test('healthy source audio stays separate from unverified public delivery', () => {
  const r = rig({ runtime: sending() });
  const actual = r.operatorSourceSnapshot();
  assert.equal(actual.sourceActive, true);
  assert.equal(actual.sourceReady, true);
  assert.equal(actual.publicVerified, false);
});

test('a process and source connection alone never prove decoded programme audio', () => {
  const r = rig({ runtime: { ...sending(), output_feed_active: false } });
  assert.equal(r.operatorSourceSnapshot().sourceReady, false);
  assert.equal(r.operatorSourceSnapshot().presentationActive, false);
});

test('stalled PCM and a blocked fanout never count as a ready source', () => {
  for (const field of ['program_pcm_stalled', 'program_fanout_blocked']) {
    const r = rig({ runtime: { ...sending(), [field]: true } });
    assert.equal(r.operatorSourceSnapshot().sourceActive, false);
  }
});

test('every configured quality branch must be healthy before source readiness', () => {
  const runtime = { ...sending(), extra_icecast_mounts: [{ branch: 'icecast:/energize-low' }] };
  const r = rig({ runtime });
  assert.equal(r.operatorSourceSnapshot().sourceReady, false);
  runtime.source_health['icecast:/energize-low'] = true;
  assert.equal(r.operatorSourceSnapshot().sourceReady, true);
});

test('the playing queue item remains visible while listener verification is pending', () => {
  const track = { id: 12, title: 'Memories', status: 'playing', duration: 211, file_path: 'H:/Music/Memories.mp3' };
  const r = rig({ runtime: { ...sending(), active_input_uri: track.file_path }, queue: [track] });
  assert.equal(r.operatorCurrentAudio(), track);
});

test('a pending queue item is never labelled as currently playing', () => {
  const r = rig({ runtime: sending(), queue: [{ title: 'Preserved song', status: 'pending' }] });
  assert.equal(r.operatorCurrentAudio(), null);
});

test('an imported special-programme file shows its actual selected title', () => {
  const runtime = { ...sending(), active_input_uri: 'file:///C:/OnAir/pool/song%20one.mp3',
    worker_loop: { running: true, last_result: { source: 'special_event', program_id: 'event1' } } };
  const programmes = [{ id: 'event1', name: 'Holiday', active_until: '2026-10-12T20:35:00Z',
    tracks: [{ path: 'C:\\OnAir\\pool\\song one.mp3', title: 'Get Lucky', duration: 0 }] }];
  const r = rig({ runtime, programmes, queue: [{ title: 'Old song', status: 'pending' }] });
  const actual = r.operatorCurrentAudio();
  assert.equal(actual.title, 'Get Lucky');
  assert.equal(actual.programme_name, 'Holiday');
  assert.equal(actual.duration, 0);
});

test('missing programme metadata still identifies the worker-owned special programme', () => {
  const r = rig({ runtime: { ...sending(), worker_loop: { last_result: { source: 'special_event', program_id: 'missing' } } } });
  assert.equal(r.operatorCurrentAudio().source, 'special_event');
});

test('a stale playing row for a different source is not shown as the current file', () => {
  const r = rig({ runtime: { ...sending(), active_input_uri: 'C:/different.mp3' },
    queue: [{ status: 'playing', title: 'Wrong song', file_path: 'C:/old.mp3' }] });
  assert.equal(r.operatorCurrentAudio(), null);
});

test('media identity preserves POSIX case and Windows file URI spaces', () => {
  const r = rig();
  assert.equal(r.normalizedOperatorMedia('file:///C:/Pool/song%20one.mp3'), r.normalizedOperatorMedia('c:\\pool\\song one.mp3'));
  assert.notEqual(r.normalizedOperatorMedia('/Music/A.mp3'), r.normalizedOperatorMedia('/Music/a.mp3'));
});

test('an already-running controller finishes start without falsely claiming an unverified listener is live', async () => {
  const r = rig({ runtime: sending() });
  r.state.stationId = 9; r.state.startArmedUntil = Date.now() + 20000;
  const commands = [], results = [];
  const health = { runtime: sending(), worker_loop: { running: true }, engine_running: true };
  Object.assign(r, {
    $: () => ({ checked: true }), window: { setTimeout: () => 1 },
    disarmStartBroadcast: () => {}, disarmStopBroadcast: () => {},
    selectedStationName: () => 'Energize', setBusy: () => {},
    setResult: (_id, text, kind) => { if (text) results.push({ text, kind }); },
    saveBroadcastAutostart: async () => {}, clearFormDirty: () => {}, asBool: value => value === true,
    api: async (url, options = {}) => {
      if (options.method === 'POST') commands.push(url);
      if (url.endsWith('/status')) return { worker_loop: { running: true } };
      if (url.startsWith('/api/health')) return health;
      if (url.startsWith('/api/settings/station')) return { settings: { broadcast_autostart_enabled: true } };
      return {};
    },
    rawFetch: async () => ({ ok: true, json: async () => ({ stations: [{ id: 9, status: 'stopped' }] }) }),
    verifiedMutation: async (mutate, read) => { await mutate(); const value = await read(); assert.equal(value.verified, true); return value; },
    renderCoreStatus: () => {}, logActivity: () => {}, toast: () => {}, loadCoreStatus: async () => {},
    errorMessage: error => error.message,
  });
  vm.runInNewContext(section(appJs, 'async function startBroadcast(', 'async function stopBroadcast('), r);
  await r.startBroadcast();
  assert.deepEqual(commands, ['/api/runtime/9/operator-supervise']);
  assert.equal(results.at(-1).kind, 'pending');
  assert.match(results.at(-1).text, /public listener verification is pending/);
  assert.doesNotMatch(results.at(-1).text, /public stream are live/);
});
