const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const root = path.resolve(__dirname, '..', '..');
const html = fs.readFileSync(
  path.join(root, 'app', 'static', 'onair', 'index.html'),
  'utf8',
);
const script = fs.readFileSync(
  path.join(root, 'app', 'static', 'onair', 'app.js'),
  'utf8',
);

function scriptSection(startMarker, endMarker) {
  const start = script.indexOf(startMarker);
  const end = script.indexOf(endMarker, start);
  assert.notEqual(start, -1, `Could not find ${startMarker}`);
  assert.notEqual(end, -1, `Could not find ${endMarker}`);
  return script.slice(start, end);
}

function classList() {
  const values = new Set();
  return {
    add(value) { values.add(value); },
    remove(value) { values.delete(value); },
    contains(value) { return values.has(value); },
  };
}

function createMouseHarness() {
  const views = ['onair', 'media', 'automation', 'emergency', 'services', 'settings', 'diagnostics'];
  const attributes = new WeakMap();
  const buttons = views.map((view) => ({
    dataset: { operatorNav: view },
    closest(selector) { return selector === '[data-operator-nav]' ? this : null; },
    setAttribute(name, value) {
      const values = attributes.get(this) || new Map();
      values.set(name, value);
      attributes.set(this, values);
    },
    removeAttribute(name) { attributes.get(this)?.delete(name); },
    getAttribute(name) { return attributes.get(this)?.get(name) || null; },
  }));
  const panels = views.map((view) => ({ dataset: { operatorView: view }, hidden: false }));
  const listeners = new Map();
  const local = new Map();
  const window = {
    location: { href: 'http://wall.test/app', hash: '' },
    history: {
      replaceState(_state, _title, next) {
        const url = new URL(String(next));
        window.location.href = url.href;
        window.location.hash = url.hash;
      },
    },
    scrollTo() {},
  };
  const elements = {
    operatorNavigation: { addEventListener(type, callback) { listeners.set(type, callback); } },
    workspaceEyebrow: { textContent: '' },
    workspaceTitle: { textContent: '', focus() {} },
    workspaceDescription: { textContent: '' },
    workspaceStation: { textContent: '' },
  };
  const document = {
    title: '',
    querySelectorAll(selector) {
      if (selector === '[data-operator-view]') return panels;
      if (selector === '[data-operator-nav]') return buttons;
      return [];
    },
    querySelector() { return null; },
    getElementById(id) { return elements[id] || null; },
  };
  const context = {
    OPERATOR_VIEWS: Object.fromEntries(views.map((view) => [view, {
      eyebrow: `${view} eyebrow`, title: `${view} title`, description: `${view} description`,
    }])),
    state: { activeView: 'onair', stationId: null },
    document,
    window,
    $: (id) => document.getElementById(id),
    localStorage: { getItem(key) { return local.get(key) || null; }, setItem(key, value) { local.set(key, value); } },
    loadOperatorViewData: async () => {},
    toast() {},
    errorMessage: (error) => String(error?.message || error),
    URL,
  };
  vm.createContext(context);
  vm.runInContext(
    `${scriptSection('function activateOperatorView(', 'function formatDuration(')}\n`
      + 'globalThis.__initializeOperatorNavigation = initializeOperatorNavigation;',
    context,
  );
  context.__initializeOperatorNavigation();
  return { buttons, panels, listeners, local, window, document, elements, state: context.state };
}

function invokeGuard(functionSource, handlerName, state, extras = {}) {
  const requests = [];
  const context = {
    state,
    Date,
    JSON,
    Object,
    Number,
    String,
    encodeURIComponent,
    window: { setTimeout() { return 1; }, clearTimeout() {} },
    document: { querySelectorAll() { return []; } },
    api: async (...args) => { requests.push(args); return {}; },
    fetch: (...args) => { requests.push(args); return Promise.resolve({}); },
    setResult() {},
    setBusy() {},
    selectedStationName() { return 'Test station'; },
    disarmStartBroadcast() {},
    disarmStopBroadcast() {},
    armEmergencyTakeover() {},
    clearEmergencyArm() {},
    clearServiceActionArm() {},
    ...extras,
  };
  vm.createContext(context);
  vm.runInContext(`${functionSource}\nglobalThis.__handler = ${handlerName};`, context);
  return { invoke: (...args) => context.__handler(...args), requests };
}

function createServiceRenderHarness(payload) {
  const nodes = new Map();
  const makeNode = () => ({
    dataset: {}, checked: false, disabled: false, hidden: false, title: '', value: '', innerHTML: '', textContent: '',
  });
  const cards = new Map();
  for (const definition of payload.definitions) {
    const id = definition.id;
    for (const field of ['enabled', 'autostart', 'source', 'config', 'health', 'backup', 'state', 'summary', 'startup-owner']) {
      nodes.set(`service-${id}-${field}`, makeNode());
    }
    const actions = { start: makeNode(), stop: makeNode(), restart: makeNode() };
    cards.set(id, {
      dataset: {},
      querySelector(selector) {
        return actions[selector.match(/"(start|stop|restart)"/)?.[1]] || null;
      },
      actions,
    });
  }
  const signature = payload.definitions.map((item) => `${item.id}:${item.startup_owner || ''}`).join('|');
  const container = {
    dataset: { signature },
    querySelector(selector) {
      return cards.get(selector.match(/data-service-card="([^"]+)"/)?.[1]) || null;
    },
    querySelectorAll() { return []; },
  };
  nodes.set('serviceControlCards', container);
  nodes.set('serviceControlState', makeNode());
  const context = {
    state: { radioteduServices: payload },
    Map,
    Date,
    JSON,
    String,
    Boolean,
    Array,
    Object,
    $: (id) => nodes.get(id) || null,
    escapeHtml(value) { return String(value ?? ''); },
    setCleanChecked(id, value) { const node = nodes.get(id); if (node && node.dataset.dirty !== '1') node.checked = Boolean(value); },
    setCleanValue(id, value) { const node = nodes.get(id); if (node && node.dataset.dirty !== '1') node.value = value ?? ''; },
  };
  vm.createContext(context);
  vm.runInContext(
    `${scriptSection('function serviceControlId(', 'async function pickRadioTEDUServicePath(')}\n`
      + 'globalThis.__render = renderRadioTEDUServices; globalThis.__collect = collectRadioTEDUServiceSettings;',
    context,
  );
  return { nodes, cards, render: context.__render, collect: context.__collect };
}

test('RadioTEDU OnAir wall is visibly branded and supports arbitrary station mounts', () => {
  assert.match(html, /RadioTEDU OnAir/);
  assert.match(html, /assets\/radiotedu-onair-logo\.png/);
  assert.match(html, /assets\/radiotedu-logo\.png/);
  assert.match(html, /id="currentIcecastMount"[^>]*pattern="\/\.\*"/);
  assert.doesNotMatch(html.match(/id="currentIcecastMount"[^>]*>/)?.[0] || '', /readonly/);
  assert.match(script, /broadcast_autostart_enabled/);
});

test('desktop folder picker uses the interactive shell bridge instead of the service API', async () => {
  const posted = [];
  let messageHandler = null;
  const requests = [];
  const context = {
    Map,
    Date,
    Promise,
    Error,
    JSON,
    String,
    Boolean,
    window: {
      chrome: {
        webview: {
          addEventListener(type, handler) {
            assert.equal(type, 'message');
            messageHandler = handler;
          },
          postMessage(message) { posted.push(message); },
        },
      },
      setTimeout() { return 77; },
      clearTimeout() {},
    },
    api: async (...args) => { requests.push(args); return {}; },
  };
  vm.createContext(context);
  vm.runInContext(
    `${scriptSection('let desktopPickerSequence', 'function activateOperatorView(')}\n`
      + 'globalThis.__pickOperatorPath = pickOperatorPath;',
    context,
  );

  const resultPromise = context.__pickOperatorPath(
    'folder',
    'D:\\Radio\\Music',
    'Select station music',
  );
  assert.equal(posted.length, 1);
  assert.equal(requests.length, 0);
  assert.equal(posted[0].type, 'radiotedu-picker-request');
  assert.equal(posted[0].kind, 'folder');
  assert.equal(posted[0].initialPath, 'D:\\Radio\\Music');
  assert.equal(typeof messageHandler, 'function');

  messageHandler({
    data: {
      type: 'radiotedu-picker-response',
      requestId: posted[0].requestId,
      selected: true,
      path: 'E:\\RadioTEDU\\LoFi',
    },
  });
  const result = await resultPromise;
  assert.equal(result.selected, true);
  assert.equal(result.folder, 'E:\\RadioTEDU\\LoFi');
  assert.equal(requests.length, 0);
});

test('ordinary browser folder picker retains the authenticated API fallback', async () => {
  const requests = [];
  const context = {
    Map,
    Date,
    Promise,
    Error,
    JSON,
    String,
    Boolean,
    window: { setTimeout() { return 1; }, clearTimeout() {} },
    api: async (...args) => {
      requests.push(args);
      return { ok: true, selected: false, folder: '' };
    },
  };
  vm.createContext(context);
  vm.runInContext(
    `${scriptSection('let desktopPickerSequence', 'function activateOperatorView(')}\n`
      + 'globalThis.__pickOperatorPath = pickOperatorPath;',
    context,
  );

  await context.__pickOperatorPath('folder', 'D:\\Radio', 'Select music');

  assert.equal(requests.length, 1);
  assert.equal(requests[0][0], '/api/operator/pick-folder');
  assert.equal(JSON.parse(requests[0][1].body).initial_folder, 'D:\\Radio');
});

test('multi-station, genre, queue, jingle, emergency, and optional AI controls are available', () => {
  assert.match(html, /class="station-control"/);
  assert.match(html, /class="status-card ai-card"/);
  assert.match(html, /class="status-card station-card"/);
  assert.match(html, /id="libraryFolderForm"/);
  assert.match(html, /id="jingleFolderForm"/);
  assert.match(html, /id="startEmergencyButton"/);
  assert.match(html, /id="aiConfigForm"/);
});

test('queue actions report durable runtime acknowledgement and refresh the live timeline', () => {
  assert.match(script, /function queueAcknowledgementText\(outcome\)/);
  assert.match(script, /Live observers were notified\./);
  assert.match(script, /has not acknowledged this queue revision yet/);
  assert.match(script, /data-queue-item-id/);
  assert.match(scriptSection('async function addTrackToQueue', 'function queueAcknowledgementText'), /renderTimeline\(\)/);
  const actions = scriptSection('async function queueAction', 'function currentUser');
  assert.match(actions, /const changed = await verifiedMutation/);
  assert.match(actions, /item_id: Number\(target\.id\)/);
  assert.match(actions, /expected_revision: expectedRevision/);
  assert.match(actions, /item_id=\$\{Number\(target\.id\)\}/);
  assert.match(actions, /queueAcknowledgementText\(changed\)/);
  assert.match(actions, /renderTimeline\(\)/);
});

test('unified media controls show root, linked-view status, refresh history, and safe rebuild action', () => {
  assert.match(html, /id="unifiedMediaRoot"/);
  assert.match(html, /id="unifiedMediaViews"/);
  assert.match(html, /id="unifiedMediaDetails"/);
  assert.match(html, /id="refreshUnifiedMediaButton"/);
  assert.match(script, /api\('\/api\/library\/unified-media\/status'\)/);
  assert.match(script, /api\('\/api\/library\/unified-media\/refresh'/);
  assert.match(script, /request_library_rescan: true/);
  assert.match(script, /function renderUnifiedMedia\(\)/);
  assert.match(script, /refreshUnifiedMediaButton'\)\.addEventListener\('click', refreshUnifiedMedia\)/);
});

test('product catalog rows expose stable state and queue only the selected product rescan', async () => {
  assert.match(html, /id="productCatalogRows"/);
  assert.match(script, /api\('\/api\/library\/product-catalog\/status'\)/);
  assert.match(script, /api\('\/api\/library\/product-catalog\/rescan'/);
  const row = { innerHTML: '' };
  const renderContext = {
    state: { productCatalog: { products: [{ product: 'ads', directory: 'Ads', state: 'retry_wait', file_count: 2, generation: 3, error_code: 'catalog_database_busy' }] } },
    $: (id) => id === 'productCatalogRows' ? row : null,
    String, Number,
    Array,
    escapeHtml(value) { return String(value); },
  };
  vm.createContext(renderContext);
  vm.runInContext(`${scriptSection('function renderProductCatalog()', 'function disarmStartBroadcast()')}\nglobalThis.__render = renderProductCatalog;`, renderContext);
  renderContext.__render();
  assert.match(row.innerHTML, /Ads/);
  assert.match(row.innerHTML, /catalog_database_busy/);
  assert.match(row.innerHTML, /data-product-catalog-rescan="ads"/);

  const requests = [];
  const actionContext = {
    state: {}, String, JSON,
    api: async (...args) => { requests.push(args); return { products: [] }; },
    setResult() {}, renderProductCatalog() {}, errorMessage(error) { return String(error); },
  };
  vm.createContext(actionContext);
  vm.runInContext(`${scriptSection('async function requestProductCatalogRescan(', 'async function loadQueue()')}\nglobalThis.__rescan = requestProductCatalogRescan;`, actionContext);
  await actionContext.__rescan('ads');
  assert.equal(requests[0][0], '/api/library/product-catalog/rescan');
  assert.equal(requests[0][1].body, JSON.stringify({ product: 'ads' }));
});

test('SCM-owned service cards retain saved auto-start while showing commissioning-gated autonomous readiness', () => {
  const payload = {
    definitions: [
      { id: 'juke_media_agent', startup_owner: 'windows_scm', kind: 'rtai_service', mounts: [], database_supported: false },
      { id: 'ollama', startup_owner: 'onair_process', kind: 'ollama', mounts: [], database_supported: false },
    ],
    services: {
      juke_media_agent: { enabled: true, auto_start: true, health_urls: [] },
      ollama: { enabled: true, auto_start: false, health_urls: [] },
    },
    status: [
      {
        id: 'juke_media_agent', startup_owner: 'windows_scm', state: 'not_ready', runtime: 'stopped', config_ready: true,
        autonomous_startup: { ready: false, state: 'verification_required', reasons: ['foreground_verification_required'] },
        health: [{ ok: true, url: 'http://127.0.0.1:3210/v1/status', status: 200, latency_ms: 4, signals: {
          library_policy: { mode: 'all_playable', rights_filter: false },
          library_roots: [{ id: 'primary', ready: true, item_count: 14883 }, { id: 'overflow', ready: true, item_count: 20930 }],
          play_ledger: { configured: true, ok: true, integrity_ok: true, durable_fsync: true, entries: 42 },
        } }],
      },
      { id: 'ollama', startup_owner: 'onair_process', state: 'healthy', runtime: 'stopped', config_ready: true },
    ],
  };
  const harness = createServiceRenderHarness(payload);
  harness.render();

  const jukeAutoStart = harness.nodes.get('service-juke_media_agent-autostart');
  const jukeOwnership = harness.nodes.get('service-juke_media_agent-startup-owner');
  assert.equal(jukeAutoStart.checked, true);
  assert.equal(jukeAutoStart.disabled, true);
  assert.equal(jukeOwnership.hidden, false);
  assert.match(jukeOwnership.innerHTML, /Windows SCM owns autonomous startup/);
  assert.match(jukeOwnership.innerHTML, /commissioning/);
  assert.match(jukeOwnership.innerHTML, /Start\/Stop remain manual controls/);
  assert.match(jukeOwnership.innerHTML, /verification required/);
  const jukeSummary = harness.nodes.get('service-juke_media_agent-summary');
  assert.match(jukeSummary.innerHTML, /Juke catalog: all_playable/);
  assert.match(jukeSummary.innerHTML, /Rights filter: disabled/);
  assert.match(jukeSummary.innerHTML, /primary 14883 \+ overflow 20930/);
  assert.match(jukeSummary.innerHTML, /Compliance ledger: healthy/);
  assert.match(jukeSummary.innerHTML, /42 permanent record/);

  jukeAutoStart.checked = false;
  const ollamaAutoStart = harness.nodes.get('service-ollama-autostart');
  ollamaAutoStart.checked = true;
  assert.equal(ollamaAutoStart.disabled, false);
  assert.equal(harness.nodes.get('service-ollama-startup-owner').hidden, true);
  const saved = harness.collect();
  assert.equal(saved.juke_media_agent.auto_start, true);
  assert.equal(saved.ollama.auto_start, true);
});

test('station context is accepted from the app URL and updated when the operator switches stations', () => {
  assert.match(script, /URLSearchParams\(window\.location\.search\).*station_id/s);
  assert.match(script, /window\.history\.replaceState\(\{\}, '', url\)/);
});

test('every JavaScript element reference exists in the wall document', () => {
  const htmlIds = new Set([...html.matchAll(/\bid="([^"]+)"/g)].map((match) => match[1]));
  const scriptIds = new Set([...script.matchAll(/\$\('([^']+)'\)/g)].map((match) => match[1]));
  const missing = [...scriptIds].filter((id) => !htmlIds.has(id));
  assert.deepEqual(missing, []);
});

test('mouse navigation activates exactly one current operator workspace and persists its hash', () => {
  const harness = createMouseHarness();
  const expectedViews = ['onair', 'media', 'playlists', 'automation', 'emergency', 'services', 'settings', 'diagnostics', 'stations', 'queue', 'scheduler', 'dayparting', 'shows', 'compliance', 'ads', 'streaming', 'recovery'];
  const markupViews = [...html.matchAll(/data-operator-nav="([^"]+)"/g)].map((match) => match[1]);

  assert.deepEqual(markupViews, expectedViews);
  assert.equal(typeof harness.listeners.get('click'), 'function');

  for (const button of harness.buttons) {
    harness.listeners.get('click')({ target: button });
    const current = button.dataset.operatorNav;
    assert.equal(harness.state.activeView, current);
    assert.equal(harness.panels.filter((panel) => !panel.hidden).length, 1);
    assert.equal(harness.panels.find((panel) => !panel.hidden).dataset.operatorView, current);
    assert.equal(harness.buttons.filter((candidate) => candidate.getAttribute('aria-current') === 'page').length, 1);
    assert.equal(button.getAttribute('aria-current'), 'page');
    assert.equal(harness.window.location.hash, `#${current}`);
    assert.equal(harness.local.get('radiotedu_onair_active_view'), current);
    assert.equal(harness.document.title, `${current} title · RadioTEDU OnAir`);
  }
});

test('first mouse click on disruptive controls only arms confirmation and sends no request', async () => {
  assert.match(script, /\$\('startBroadcastButton'\)\.addEventListener\('click', startBroadcast\)/);
  assert.match(script, /\$\('stopBroadcastButton'\)\.addEventListener\('click', stopBroadcast\)/);
  assert.match(script, /\$\('startEmergencyButton'\)\.addEventListener\('click', startEmergency\)/);
  assert.match(script, /serviceButton\) controlRadioTEDUService\(serviceButton\)/);

  const startButton = { textContent: 'Start broadcast' };
  const start = invokeGuard(
    scriptSection('async function startBroadcast()', 'async function stopBroadcast()'),
    'startBroadcast',
    { startArmedUntil: 0 },
    { $: (id) => id === 'startBroadcastButton' ? startButton : { checked: false } },
  );
  await start.invoke();
  assert.deepEqual(start.requests, []);
  assert.equal(startButton.textContent, 'Confirm start broadcast');

  const stopButton = { textContent: 'Stop stream — keep playlist' };
  const stop = invokeGuard(
    scriptSection('async function stopBroadcast()', 'async function setAiEnabled('),
    'stopBroadcast',
    { stopArmedUntil: 0 },
    { $: (id) => id === 'stopBroadcastButton' ? stopButton : { checked: false } },
  );
  await stop.invoke();
  assert.deepEqual(stop.requests, []);
  assert.equal(stopButton.textContent, 'Confirm stop — keep playlist');

  let emergencyArms = 0;
  const emergency = invokeGuard(
    scriptSection('async function startEmergency()', 'async function stopEmergency('),
    'startEmergency',
    { stationId: 1, emergency: { active: false, starting: false, armedUntil: 0 } },
    { armEmergencyTakeover() { emergencyArms += 1; } },
  );
  await emergency.invoke();
  assert.equal(emergencyArms, 1);
  assert.deepEqual(emergency.requests, []);

  const serviceButton = {
    dataset: { serviceId: 'ollama', serviceAction: 'stop' },
    textContent: 'Stop service',
    classList: classList(),
  };
  const service = invokeGuard(
    scriptSection('async function controlRadioTEDUService(', 'async function publishVotingRound('),
    'controlRadioTEDUService',
    { serviceActionArmed: {} },
  );
  await service.invoke(serviceButton);
  assert.deepEqual(service.requests, []);
  assert.equal(serviceButton.classList.contains('armed'), true);
  assert.equal(serviceButton.textContent, 'Confirm Stop service');
});
