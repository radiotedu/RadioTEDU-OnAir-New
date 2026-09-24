const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const root = path.resolve(__dirname, '..', '..');
const script = fs.readFileSync(path.join(root, 'app', 'static', 'onair', 'app.js'), 'utf8');

function scriptSection(startMarker, endMarker) {
  const start = script.indexOf(startMarker);
  const end = script.indexOf(endMarker, start);
  assert.notEqual(start, -1, `Could not find ${startMarker}`);
  assert.notEqual(end, -1, `Could not find ${endMarker}`);
  return script.slice(start, end);
}

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

function createCoreHarness() {
  const oldStationGate = deferred();
  let publicStatusRequest = 0;
  const state = {
    stationId: 4,
    health: null,
    runtime: null,
    publicStation: null,
    ai: null,
    sweeper: null,
    dayparts: null,
    stationSettings: null,
    stationOutput: null,
    libraryWatcher: null,
    productCatalog: null,
    stationStatusLoading: true,
  };
  const stationResponse = (url, stationId) => {
    if (url.startsWith('/api/health')) return { station_id: stationId, output_mode: 'icecast', runtime: { running: true } };
    if (url.includes('/api/runtime/')) return { station_id: stationId, running: true };
    if (url.startsWith('/api/ai/')) return { station_id: stationId, ai_host_enabled: true };
    if (url.startsWith('/api/sweeper/')) return { station_id: stationId, enabled: true, interval: 2 };
    if (url.startsWith('/api/dayparts')) return { station_id: stationId };
    if (url.startsWith('/api/settings/station')) return { settings: { station_id: stationId } };
    if (url.startsWith('/api/stations/output')) return { station_id: stationId };
    return { station_id: stationId };
  };
  const context = {
    state,
    Date,
    Promise,
    Number,
    String,
    Boolean,
    Array,
    Object,
    IS_RTAI_ONAIR: false,
    api: async (url) => {
      const match = String(url).match(/station_id=(\d+)|\/runtime\/(\d+)\//);
      const stationId = Number(match?.[1] || match?.[2] || 0);
      if (stationId === 4) await oldStationGate.promise;
      return stationResponse(String(url), stationId);
    },
    rawFetch: async () => {
      const requestNumber = ++publicStatusRequest;
      if (requestNumber === 1) await oldStationGate.promise;
      const stationId = requestNumber === 1 ? 4 : 11;
      return {
        ok: true,
        json: async () => ({ stations: [{ id: stationId, status: 'live', now_playing: { title: `station-${stationId}` } }] }),
      };
    },
    renderCoreStatus() {},
    renderLibraryProfile() {},
    renderUnifiedMedia() {},
    renderProductCatalog() {},
    renderOutputConfiguration() {},
    renderAiConfiguration() {},
    renderDayparts() {},
    renderTimeline() {},
    renderEmergencyStatus() {},
  };
  vm.createContext(context);
  vm.runInContext(
    `let stationContextRevision = 0; let coreStatusRequestSequence = 0;\n`
      + 'function isCurrentStationContext(stationId, revision = stationContextRevision) { return Number(stationId) === Number(state.stationId) && Number(revision) === Number(stationContextRevision); }\n'
      + `${scriptSection('async function loadCoreStatus()', 'async function loadOperatorConfiguration()')}\n`
      + 'globalThis.__loadCoreStatus = loadCoreStatus; globalThis.__advanceStation = () => { stationContextRevision += 1; };',
    context,
  );
  return { context, state, oldStationGate };
}

test('late core status for the previous station cannot overwrite the selected station', async () => {
  const { context, state, oldStationGate } = createCoreHarness();
  const previousStationRequest = context.__loadCoreStatus();
  state.stationId = 11;
  context.__advanceStation();
  state.stationStatusLoading = true;
  await context.__loadCoreStatus();

  assert.equal(state.health.station_id, 11);
  assert.equal(state.publicStation.id, 11);
  assert.equal(state.stationStatusLoading, false);

  oldStationGate.resolve();
  await previousStationRequest;
  assert.equal(state.health.station_id, 11);
  assert.equal(state.publicStation.id, 11);
});

test('late queue response for the previous station cannot replace the selected station queue', async () => {
  const oldQueueGate = deferred();
  const state = { stationId: 4, queue: [], queueRevision: '' };
  let stationContextRevision = 0;
  let queueRequestSequence = 0;
  const context = {
    state,
    Promise,
    Number,
    String,
    Array,
    api: async (url) => {
      if (String(url).includes('station_id=4')) await oldQueueGate.promise;
      const stationId = Number(String(url).match(/station_id=(\d+)/)?.[1]);
      return { items: [{ title: `station-${stationId}` }], revision: String(stationId) };
    },
    renderQueue() {},
    renderTimeline() {},
  };
  vm.createContext(context);
  vm.runInContext(
    `let stationContextRevision = ${stationContextRevision}; let queueRequestSequence = ${queueRequestSequence};\n`
      + 'function isCurrentStationContext(stationId, revision = stationContextRevision) { return Number(stationId) === Number(state.stationId) && Number(revision) === Number(stationContextRevision); }\n'
      + `${scriptSection('async function loadQueue()', 'function renderQueue()')}\n`
      + 'globalThis.__loadQueue = loadQueue; globalThis.__advanceStation = () => { stationContextRevision += 1; };',
    context,
  );
  const previousStationRequest = context.__loadQueue();
  state.stationId = 11;
  context.__advanceStation();
  await context.__loadQueue();
  assert.equal(state.queue[0].title, 'station-11');

  oldQueueGate.resolve();
  await previousStationRequest;
  assert.equal(state.queue[0].title, 'station-11');
  assert.equal(state.queueRevision, '11');
});

test('a previous refresh failure cannot mark the newly selected station offline', async () => {
  const oldRefreshGate = deferred();
  let refreshCalls = 0;
  const state = { stationId: 4, busy: false, activeView: 'onair' };
  const connections = [];
  const context = {
    state,
    Promise,
    Number,
    String,
    IS_RTAI_ONAIR: true,
    loadCoreStatus: async () => {},
    loadQueue: async () => {
      refreshCalls += 1;
      if (refreshCalls === 1) await oldRefreshGate.promise;
    },
    setConnection: (...args) => connections.push(args),
    toast() {},
    errorMessage: (error) => String(error?.message || error),
  };
  vm.createContext(context);
  vm.runInContext(
    'let stationContextRevision = 0; let connectionRefreshSequence = 0; let stationRefreshInFlight = false;\n'
      + 'function isCurrentStationContext(stationId, revision = stationContextRevision) { return Number(stationId) === Number(state.stationId) && Number(revision) === Number(stationContextRevision); }\n'
      + `${scriptSection('async function refreshAll(', 'async function saveBroadcastAutostart(')}\n`
      + 'globalThis.__refreshAll = refreshAll; globalThis.__advanceStation = () => { stationContextRevision += 1; connectionRefreshSequence += 1; };',
    context,
  );
  const previousRefresh = context.__refreshAll(true);
  state.stationId = 11;
  context.__advanceStation();
  assert.equal(await context.__refreshAll(true), false);
  oldRefreshGate.reject(new Error('old station request failed'));
  await previousRefresh;
  await context.__refreshAll(true);

  assert.deepEqual(connections, [['online', 'Backend connected']]);
});

test('periodic status refresh does not overlap a poll that is still running', async () => {
  const delayedStatus = deferred();
  let statusCalls = 0;
  let intervalCallback = null;
  const state = { stationId: 4, busy: false, refreshTimer: null };
  const connections = [];
  const context = {
    state,
    Promise,
    Number,
    document: { hidden: false },
    window: {
      setInterval(callback) { intervalCallback = callback; return 1; },
      clearInterval() {},
    },
    loadCoreStatus: () => {
      statusCalls += 1;
      return statusCalls === 1 ? delayedStatus.promise : Promise.resolve();
    },
    loadQueue: async () => {},
    setConnection: (...args) => connections.push(args),
  };
  vm.createContext(context);
  vm.runInContext(
    'let stationContextRevision = 0; let connectionRefreshSequence = 0; let stationRefreshInFlight = false;\n'
      + 'function isCurrentStationContext(stationId, revision = stationContextRevision) { return Number(stationId) === Number(state.stationId) && Number(revision) === Number(stationContextRevision); }\n'
      + 'function stopRefreshTimer() { state.refreshTimer = null; }\n'
      + `${scriptSection('function startRefreshTimer()', 'function stopRefreshTimer()')}\n`
      + 'globalThis.__startRefreshTimer = startRefreshTimer;',
    context,
  );

  context.__startRefreshTimer();
  intervalCallback();
  intervalCallback();
  assert.equal(statusCalls, 1);

  delayedStatus.resolve();
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(connections.length, 1);
  intervalCallback();
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(statusCalls, 2);
  assert.equal(connections.length, 2);
  assert.ok(connections.every(([mode, label]) => mode === 'online' && label === 'Backend connected'));
});
