'use strict';

const AUTH_KEYS = Object.freeze({
  access: 'cleanroom_auth_access_token',
  refresh: 'cleanroom_auth_refresh_token',
  user: 'cleanroom_auth_user',
});
let PRODUCT_EDITION = String(document.querySelector('meta[name="onair-edition"]')?.content || 'radiotedu').trim().toLowerCase();
let IS_RTAI_ONAIR = ['rtai', 'rtai-onair', 'rtai_onair'].includes(PRODUCT_EDITION);
const IDLE_TIMEOUT_MS = 15 * 60 * 1000;
const IDLE_WARNING_MS = 60 * 1000;
const OPERATOR_VIEWS = Object.freeze({
  onair: { eyebrow: 'LIVE CONTROL', title: 'On Air', description: 'Control the active broadcast and see what plays next.' },
  stations: { eyebrow: 'STATION FLEET', title: 'Stations', description: 'Create stations and configure independent broadcast outputs.' },
  media: { eyebrow: 'MEDIA LIBRARY', title: 'Media', description: 'Import, validate, and find station audio.' },
  playlists: { eyebrow: 'REUSABLE PROGRAMMING', title: 'Playlists', description: 'Create reusable track lists and verify every ordering change.' },
  queue: { eyebrow: 'DURABLE PLAYOUT', title: 'Queue', description: 'Inspect, reorder, skip, and verify the selected station queue.' },
  scheduler: { eyebrow: 'EXACT-TIME EVENTS', title: 'Scheduler', description: 'Schedule station tracks inside explicit playout windows.' },
  dayparting: { eyebrow: 'WEEKLY CLOCK', title: 'Dayparting', description: 'Define timezone-aware tempo programs for every day.' },
  automation: { eyebrow: 'DETERMINISTIC RULES', title: 'Automation', description: 'Manage jingles and exact, operator-defined insertion rules.' },
  emergency: { eyebrow: 'PRIORITY TAKEOVER', title: 'Emergency Broadcast', description: 'Preview and broadcast an approved external public-service source.' },
  services: { eyebrow: 'OPTIONAL SYSTEMS', title: 'Services', description: 'Control Ollama, RadioTEDU AI, Voting, Juke, and their databases.' },
  settings: { eyebrow: 'SECURE ADMINISTRATION', title: 'Settings', description: 'Manage the signed-in operator account and global policy.' },
  diagnostics: { eyebrow: 'RELIABILITY', title: 'Diagnostics', description: 'Run readiness checks and review operator activity.' },
  shows: { eyebrow: 'PROGRAM OPERATIONS', title: 'Shows', description: 'Manage programs, presenters, transition audio, and live show state.' },
  compliance: { eyebrow: 'RIGHTS & REPORTING', title: 'Compliance', description: 'Review permanent music-use records, complete track documentation, and close reporting periods.' },
  ads: { eyebrow: 'ADVERTISING OPERATIONS', title: 'Advertising', description: 'Schedule approved spots and manage break clocks and campaigns.' },
  streaming: { eyebrow: 'ORIGIN OPERATIONS', title: 'Streaming', description: 'Check origin health and safely manage server-side streaming features.' },
  recovery: { eyebrow: 'BACKUP & RECOVERY', title: 'Backup / Recovery', description: 'Create and verify protected database recovery points.' },
});

const state = {
  stationId: null,
  stations: [],
  studios: [],
  selectedStudioId: 0,
  joinedStudioId: 0,
  users: [],
  roleTemplates: [],
  permissionGroups: {},
  adminAccessLoaded: false,
  userDeactivateArmedUntil: 0,
  roleDeactivateArmedUntil: 0,
  health: null,
  runtime: null,
  publicStation: null,
  ai: null,
  stationSettings: null,
  stationOutput: null,
  speakerMonitor: null,
  startupSound: null,
  libraryWatcher: null,
  unifiedMedia: null,
  productCatalog: null,
  campaign: null,
  integrations: null,
  radioteduServices: null,
  jukeLibrary: null,
  jukeLibraryActionArmed: {},
  watchdog: null,
  serviceActionArmed: {},
  activeView: 'onair',
  setupState: null,
  audioDevices: [],
  sweeper: null,
  dayparts: null,
  scheduleItems: [],
  recoveryPoints: [],
  shows: [],
  selectedShowId: 0,
  showAssignments: [],
  showCandidates: [],
  showSession: null,
  showDeleteArmedUntil: 0,
  guestRecordings: [],
  guestRecordingDeleteArmed: {},
  musicUsage: [],
  musicClosures: [],
  adItems: [],
  adRuntime: null,
  adBreakSets: [],
  selectedAdBreakSetId: 0,
  adCampaigns: [],
  selectedAdCampaignId: 0,
  adDeleteArmed: {},
  hlsSettings: null,
  streamingFeatures: null,
  streamingHealth: null,
  qualityOutputs: null,
  streamingActionArmed: {},
  diagnosticBundles: [],
  queue: [],
  queueRevision: '',
  playlists: [],
  selectedPlaylistId: 0,
  playlistItems: [],
  playlistDeleteArmedUntil: 0,
  library: [],
  ytdlpSettings: null,
  ytdlpJobs: null,
  ytdlpPollTimer: null,
  metadataRules: [],
  metadataRuleDeleteArmed: {},
  metadataMaintenanceArmedUntil: 0,
  libraryPage: 1,
  libraryPages: 1,
  libraryTotal: 0,
  jingles: [],
  busy: false,
  refreshTimer: null,
  timelineTimer: null,
  idleTimer: null,
  lastUserActivityAt: 0,
  timelineAnchorAt: 0,
  startArmedUntil: 0,
  startArmTimer: null,
  stopArmedUntil: 0,
  stopArmTimer: null,
  stationDeleteArmedUntil: 0,
  stationDeleteArmTimer: null,
  emergency: {
    active: false,
    starting: false,
    stopping: false,
    stationId: null,
    stream: null,
    audioContext: null,
    sourceNode: null,
    processorNode: null,
    silentGainNode: null,
    pendingChunks: [],
    draining: false,
    droppedChunks: 0,
    originalSettings: null,
    statusTimer: null,
    openedWindow: null,
    armedUntil: 0,
    armTimer: null,
    sourceUrl: '',
  },
};
// Small companion panels are loaded as separate deferred scripts. Expose the
// shared UI state deliberately so they do not spin on ReferenceError and flood
// the browser/backend every two seconds.
globalThis.radioTEDUOnAirState = state;

const $ = (id) => document.getElementById(id);
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const asBool = (value) => value === true || ['1', 'true', 'yes', 'on'].includes(String(value || '').toLowerCase());

function hideEditionPanel(id) {
  const element = $(id);
  const panel = element?.closest('article') || element;
  if (panel) panel.hidden = true;
}

async function loadProductEditionProfile() {
  try {
    const response = await rawFetch('/api/product-profile', { cache: 'no-store' }, 8000);
    if (!response.ok) return;
    const profile = await response.json();
    PRODUCT_EDITION = String(profile?.edition || PRODUCT_EDITION).trim().toLowerCase();
    IS_RTAI_ONAIR = ['rtai', 'rtai-onair', 'rtai_onair'].includes(PRODUCT_EDITION);
  } catch (_) {
    // The static meta profile keeps the login usable if the backend is still
    // starting. The next full reload reads the authoritative local profile.
  }
}

function applyProductEdition() {
  if (!IS_RTAI_ONAIR) return;
  document.documentElement.dataset.productEdition = 'rtai-onair';
  document.title = 'rtAI OnAir';
  const description = document.querySelector('meta[name="description"]');
  if (description) description.content = 'Deterministic local-first rtAI OnAir broadcast automation';

  document.querySelectorAll('.onair-program-logo, .onair-brand-logo, .founder-lockup, .compact-founder-lockup').forEach((element) => { element.hidden = true; });
  const loginEyebrow = document.querySelector('#loginForm .eyebrow');
  const loginTitle = document.querySelector('#loginForm h1');
  const brandEyebrow = document.querySelector('.brand-block .eyebrow');
  const brandTitle = document.querySelector('.brand-block h1');
  if (loginEyebrow) loginEyebrow.textContent = 'LOCAL-FIRST BROADCAST AUTOMATION';
  if (loginTitle) loginTitle.textContent = 'rtAI OnAir sign in';
  if (brandEyebrow) brandEyebrow.textContent = 'LOCAL-FIRST AUTOMATION';
  if (brandTitle) brandTitle.textContent = 'rtAI OnAir';
  const brandLockup = document.querySelector('.brand-lockup');
  if (brandLockup) brandLockup.setAttribute('aria-label', 'rtAI OnAir');

  const servicesButton = document.querySelector('[data-operator-nav="services"] small');
  if (servicesButton) servicesButton.textContent = 'Local AI and readiness';
  OPERATOR_VIEWS.services.description = 'Control the local AI host and verify offline readiness.';

  [
    'hlsHomeCard',
    'campaignForm',
    'integrationForm',
    'unifiedMediaState',
    'qualityOutputsPanel',
    'hlsSettingsForm',
  ].forEach(hideEditionPanel);
}

let desktopPickerSequence = 0;
let desktopPickerInitialized = false;
const desktopPickerPending = new Map();

function desktopPickerWebView() {
  const webview = window.chrome?.webview;
  return webview
    && typeof webview.postMessage === 'function'
    && typeof webview.addEventListener === 'function'
    ? webview
    : null;
}

function handleDesktopPickerMessage(event) {
  let message = event?.data;
  if (typeof message === 'string') {
    try { message = JSON.parse(message); } catch (_) { return; }
  }
  if (message?.type !== 'radiotedu-picker-response') return;
  const pending = desktopPickerPending.get(String(message.requestId || ''));
  if (!pending) return;
  desktopPickerPending.delete(String(message.requestId));
  window.clearTimeout(pending.timer);
  if (message.error) {
    pending.reject(new Error(String(message.error)));
    return;
  }
  const selectedPath = String(message.path || '');
  pending.resolve({
    ok: true,
    selected: Boolean(message.selected && selectedPath),
    path: selectedPath,
    folder: selectedPath,
  });
}

function initializeDesktopPickerBridge() {
  const webview = desktopPickerWebView();
  if (!webview) return false;
  if (!desktopPickerInitialized) {
    webview.addEventListener('message', handleDesktopPickerMessage);
    desktopPickerInitialized = true;
  }
  return true;
}

function requestDesktopPicker(kind, initialPath, description) {
  const webview = desktopPickerWebView();
  if (!webview || !initializeDesktopPickerBridge()) {
    return Promise.reject(new Error('RadioTEDU desktop picker bridge is unavailable'));
  }
  const requestId = `picker-${Date.now()}-${++desktopPickerSequence}`;
  return new Promise((resolve, reject) => {
    const timer = window.setTimeout(() => {
      desktopPickerPending.delete(requestId);
      reject(new Error('The desktop folder window timed out. Enter the absolute path instead.'));
    }, 620000);
    desktopPickerPending.set(requestId, { resolve, reject, timer });
    webview.postMessage({
      type: 'radiotedu-picker-request',
      requestId,
      kind,
      initialPath: String(initialPath || ''),
      description: String(description || ''),
    });
  });
}

async function pickOperatorPath(kind, initialPath, description) {
  if (initializeDesktopPickerBridge()) {
    return requestDesktopPicker(kind, initialPath, description);
  }
  const endpoint = kind === 'file'
    ? '/api/operator/pick-file'
    : '/api/operator/pick-folder';
  return api(endpoint, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(kind === 'file'
      ? { initial_path: String(initialPath || ''), description }
      : { initial_folder: String(initialPath || ''), description }),
    timeoutMs: 620000,
  });
}

function activateOperatorView(requestedView, { persist = true, focus = false } = {}) {
  const view = OPERATOR_VIEWS[requestedView] ? requestedView : 'onair';
  state.activeView = view;
  document.querySelectorAll('[data-operator-view]').forEach((node) => {
    node.hidden = node.dataset.operatorView !== view || node.dataset.accessHidden === 'true';
  });
  document.querySelectorAll('[data-operator-nav]').forEach((button) => {
    const active = button.dataset.operatorNav === view;
    if (active) button.setAttribute('aria-current', 'page');
    else button.removeAttribute('aria-current');
  });
  const definition = OPERATOR_VIEWS[view];
  $('workspaceEyebrow').textContent = definition.eyebrow;
  $('workspaceTitle').textContent = definition.title;
  $('workspaceDescription').textContent = definition.description;
  $('workspaceStation').textContent = state.stationId ? `Active: ${selectedStationName()}` : 'No station selected';
  document.title = `${definition.title} · RadioTEDU OnAir`;
  if (persist) {
    localStorage.setItem('radiotedu_onair_active_view', view);
    const url = new URL(window.location.href);
    url.hash = view;
    window.history.replaceState({}, '', url);
  }
  if (focus) $('workspaceTitle').focus?.({ preventScroll: true });
  window.scrollTo({ top: 0, behavior: 'auto' });
}

function initializeOperatorNavigation() {
  const stationCard = document.querySelector('.station-card');
  const settingsGroup = document.querySelector('.configuration-grid');
  if (stationCard && settingsGroup) settingsGroup.appendChild(stationCard);
  const hashView = String(window.location.hash || '').replace(/^#/, '');
  const savedView = localStorage.getItem('radiotedu_onair_active_view') || '';
  activateOperatorView(OPERATOR_VIEWS[hashView] ? hashView : savedView, { persist: false });
  $('operatorNavigation').addEventListener('click', (event) => {
    const button = event.target.closest('[data-operator-nav]');
    if (button) {
      const view = button.dataset.operatorNav;
      activateOperatorView(view, { focus: true });
      loadOperatorViewData(view).catch((error) => toast(errorMessage(error), 'error'));
    }
  });
}

function formatDuration(seconds, empty = '--:--') {
  const total = Number(seconds);
  if (!Number.isFinite(total) || total < 0) return empty;
  const rounded = Math.max(0, Math.ceil(total));
  const hours = Math.floor(rounded / 3600);
  const minutes = Math.floor((rounded % 3600) / 60);
  const secs = rounded % 60;
  return hours > 0
    ? `${hours}:${String(minutes).padStart(2, '0')}:${String(secs).padStart(2, '0')}`
    : `${minutes}:${String(secs).padStart(2, '0')}`;
}

function formatClock(value) {
  const date = value instanceof Date ? value : new Date(value);
  if (Number.isNaN(date.getTime())) return '--:--:--';
  return date.toLocaleTimeString([], { hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' });
}

function estimatedClockDate(value, reference = new Date()) {
  const match = String(value || '').match(/^(\d{1,2}):(\d{2})(?::(\d{2}))?$/);
  if (!match) return null;
  const candidate = new Date(reference);
  candidate.setHours(Number(match[1]), Number(match[2]), Number(match[3] || 0), 0);
  if (candidate.getTime() < reference.getTime() - 6 * 60 * 60 * 1000) candidate.setDate(candidate.getDate() + 1);
  if (candidate.getTime() > reference.getTime() + 18 * 60 * 60 * 1000) candidate.setDate(candidate.getDate() - 1);
  return candidate;
}

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>'"]/g, (character) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' })[character]);
}

function errorMessage(error) {
  if (!error) return 'Unknown error';
  const message = String(error.message || error).trim();
  return message.length > 260 ? `${message.slice(0, 257)}…` : message;
}

function setResult(id, message = '', type = '') {
  const node = $(id);
  if (!node) return;
  node.textContent = message;
  node.className = `action-result${type ? ` ${type}` : ''}`;
}

function toast(message, type = 'success') {
  const node = document.createElement('div');
  node.className = `toast${type === 'error' ? ' error' : ''}`;
  node.textContent = message;
  $('toastRegion').appendChild(node);
  window.setTimeout(() => node.remove(), 5200);
}

function logActivity(message, type = 'success') {
  const node = document.createElement('li');
  if (type === 'error') node.className = 'error';
  node.innerHTML = `${escapeHtml(message)}<time>${new Date().toLocaleTimeString([], { hour12: false })}</time>`;
  $('activityList').prepend(node);
  while ($('activityList').children.length > 30) $('activityList').lastElementChild.remove();
}

function setBusy(enabled, title = 'Working…', detail = 'Waiting for verified state') {
  state.busy = Boolean(enabled);
  $('busyOverlay').hidden = !enabled;
  $('busyTitle').textContent = title;
  $('busyDetail').textContent = detail;
  document.querySelectorAll('button').forEach((button) => {
    if (enabled) {
      button.dataset.beforeBusy = button.disabled ? '1' : '0';
      button.disabled = true;
    } else if (button.dataset.beforeBusy !== '1') {
      button.disabled = false;
      delete button.dataset.beforeBusy;
    } else {
      delete button.dataset.beforeBusy;
    }
  });
  if (!enabled) {
    syncActionButtons();
    if (state.radioteduServices) renderRadioTEDUServices();
  }
}

function setConnection(mode, label) {
  const node = $('connectionState');
  node.className = `connection-pill ${mode}`;
  node.innerHTML = `<span></span>${escapeHtml(label)}`;
}

function parseResponseError(text, status, requestId = '') {
  let detail = text;
  try {
    const data = JSON.parse(text);
    const raw = data.detail ?? data.message ?? data.error ?? text;
    detail = typeof raw === 'string' ? raw : JSON.stringify(raw);
  } catch (_) { /* Keep response text. */ }
  const suffix = requestId ? ` [request ${requestId}]` : '';
  const error = new Error(`${status}: ${String(detail || 'Request failed')}${suffix}`);
  error.status = Number(status || 0);
  error.requestId = requestId;
  return error;
}

async function rawFetch(url, options = {}, timeoutMs = 20000) {
  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), timeoutMs);
  try {
    return await fetch(url, { cache: 'no-store', ...options, signal: controller.signal });
  } catch (error) {
    if (error.name === 'AbortError') throw new Error(`Request timed out after ${Math.round(timeoutMs / 1000)} seconds`);
    throw error;
  } finally {
    window.clearTimeout(timer);
  }
}

function saveSession(payload) {
  if (payload.access_token) localStorage.setItem(AUTH_KEYS.access, String(payload.access_token));
  if (payload.refresh_token) localStorage.setItem(AUTH_KEYS.refresh, String(payload.refresh_token));
  if (payload.user) localStorage.setItem(AUTH_KEYS.user, JSON.stringify(payload.user));
}

function clearSession() {
  Object.values(AUTH_KEYS).forEach((key) => localStorage.removeItem(key));
}

let refreshSessionPromise = null;

async function refreshSession() {
  if (refreshSessionPromise) return refreshSessionPromise;
  refreshSessionPromise = (async () => {
    const refreshToken = localStorage.getItem(AUTH_KEYS.refresh);
    if (!refreshToken) return false;
    const response = await rawFetch('/api/auth/refresh', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ refresh_token: refreshToken }),
    }, 12000);
    if (!response.ok) return false;
    saveSession(await response.json());
    return true;
  })();
  try {
    return await refreshSessionPromise;
  } finally {
    refreshSessionPromise = null;
  }
}

async function api(url, options = {}, retry = true) {
  const method = String(options.method || 'GET').toUpperCase();
  const maxAttempts = Math.max(1, Number(options.transportAttempts || (method === 'GET' || options.idempotent ? 3 : 1)));
  const requestOptions = { ...options };
  delete requestOptions.timeoutMs;
  delete requestOptions.transportAttempts;
  delete requestOptions.idempotent;
  const headers = new Headers(options.headers || {});
  const token = localStorage.getItem(AUTH_KEYS.access);
  if (token) headers.set('Authorization', `Bearer ${token}`);
  let response;
  let lastError;
  for (let attempt = 0; attempt < maxAttempts; attempt += 1) {
    try {
      response = await rawFetch(url, { ...requestOptions, headers }, options.timeoutMs || 20000);
      if (![502, 503, 504].includes(response.status) || attempt >= maxAttempts - 1) break;
      lastError = parseResponseError(await response.text(), response.status, response.headers.get('X-Request-ID') || '');
    } catch (error) {
      lastError = error;
      if (attempt >= maxAttempts - 1) throw error;
    }
    await sleep(350 * (attempt + 1));
  }
  if (!response) throw lastError || new Error('Backend did not return a response');
  if (response.status === 401 && retry && await refreshSession()) return api(url, options, false);
  const text = await response.text();
  if (!response.ok) throw parseResponseError(text, response.status, response.headers.get('X-Request-ID') || '');
  if (!text) return null;
  try { return JSON.parse(text); } catch (_) { return text; }
}

async function poll(check, { attempts = 20, interval = 500, description = 'state' } = {}) {
  let lastValue;
  for (let index = 0; index < attempts; index += 1) {
    lastValue = await check();
    if (lastValue && lastValue.verified) return lastValue.value;
    await sleep(interval);
  }
  throw new Error(`Timed out verifying ${description}`);
}

async function verifiedMutation(mutate, verify, options = {}) {
  let mutationResult;
  let mutationError = null;
  try {
    mutationResult = await mutate();
  } catch (error) {
    mutationError = error;
  }
  try {
    const value = await poll(async () => {
      try {
        return await verify();
      } catch (_) {
        return { verified: false };
      }
    }, options);
    return { mutationResult, value, recoveredTransportError: Boolean(mutationError) };
  } catch (verificationError) {
    throw mutationError || verificationError;
  }
}

function selectedStationName() {
  return state.stations.find((station) => Number(station.id) === Number(state.stationId))?.name || `Station ${state.stationId}`;
}

function deterministicRotationKey(settings = state.stationSettings || {}) {
  return String(settings.autoplay_shuffle_seed || `radiotedu-onair-station-${Number(state.stationId)}`).trim();
}

function streamProfileBitrate(profile) {
  return ({ aac_low_192: 192, aac_he_v2_64: 64, he_aac_96: 96, he_aac_192: 192, opus_32: 32, opus_64: 64, opus_96: 96, opus_192: 192 })[profile] || 192;
}

async function ensureSignedIn() {
  if (!localStorage.getItem(AUTH_KEYS.access)) return false;
  try {
    await api('/api/auth/me');
    return true;
  } catch (_) {
    clearSession();
    return false;
  }
}

async function login(event) {
  event.preventDefault();
  $('loginButton').disabled = true;
  $('loginError').textContent = '';
  try {
    const response = await rawFetch('/api/auth/login', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username: $('loginUsername').value.trim(), password: $('loginPassword').value }),
    }, 12000);
    const text = await response.text();
    if (!response.ok) throw parseResponseError(text, response.status, response.headers.get('X-Request-ID') || '');
    saveSession(JSON.parse(text));
    $('loginPassword').value = '';
    await showApp();
  } catch (error) {
    $('loginError').textContent = errorMessage(error);
  } finally {
    $('loginButton').disabled = false;
  }
}

async function showApp() {
  $('authGate').hidden = true;
  $('appShell').hidden = false;
  await loadStations();
  await loadAdminAccess();
  await refreshAll(true);
  await loadDiagnosticBundles();
  await loadOperatorViewData(state.activeView);
  startRefreshTimer();
  startTimelineTimer();
  startIdleTimer();
}

function showLogin() {
  stopRefreshTimer();
  stopTimelineTimer();
  stopIdleTimer();
  state.adminAccessLoaded = false;
  $('appShell').hidden = true;
  $('authGate').hidden = false;
  $('loginUsername').focus();
}

function recordUserActivity() {
  if ($('appShell').hidden) return;
  state.lastUserActivityAt = Date.now();
  $('idleTimeoutBanner').hidden = true;
}

async function expireIdleSession() {
  stopIdleTimer();
  try { await api('/api/auth/logout', { method: 'POST' }); } catch (_) { /* local lock still succeeds */ }
  clearSession();
  showLogin();
  $('loginError').textContent = 'Your operator session locked after 15 minutes without activity. Sign in to continue.';
}

function startIdleTimer() {
  stopIdleTimer();
  state.lastUserActivityAt = Date.now();
  state.idleTimer = window.setInterval(() => {
    const remaining = IDLE_TIMEOUT_MS - (Date.now() - state.lastUserActivityAt);
    if (remaining <= 0) {
      expireIdleSession();
      return;
    }
    const warning = remaining <= IDLE_WARNING_MS;
    $('idleTimeoutBanner').hidden = !warning;
    if (warning) $('idleTimeoutCountdown').textContent = String(Math.max(1, Math.ceil(remaining / 1000)));
  }, 1000);
}

function stopIdleTimer() {
  if (state.idleTimer) window.clearInterval(state.idleTimer);
  state.idleTimer = null;
  $('idleTimeoutBanner').hidden = true;
}

async function logout() {
  if (state.emergency.active || state.emergency.starting) await stopEmergency('sign out').catch(() => {});
  try { await api('/api/auth/logout', { method: 'POST' }); } catch (_) { /* local logout still succeeds */ }
  clearSession();
  showLogin();
}

async function loadStations(preferredId = null) {
  const previousStationId = Number(state.stationId || 0);
  const [stationsPayload, activePayload] = await Promise.all([api('/api/stations'), api('/api/stations/active')]);
  state.stations = Array.isArray(stationsPayload?.stations) ? stationsPayload.stations : [];
  const requested = Number(new URLSearchParams(window.location.search).get('station_id') || 0);
  const legacySaved = localStorage.getItem('deterministic_wall_station_id');
  const saved = Number(localStorage.getItem('radiotedu_onair_station_id') || legacySaved || 0);
  const candidate = Number(preferredId || requested || state.stationId || saved || activePayload?.station_id || state.stations[0]?.id || 0);
  state.stationId = state.stations.some((station) => Number(station.id) === candidate) ? candidate : Number(state.stations[0]?.id || 0);
  if (!state.stationId) throw new Error('No station is available');
  if (previousStationId && previousStationId !== Number(state.stationId)) {
    state.studios = [];
    state.selectedStudioId = 0;
    state.joinedStudioId = 0;
  }
  localStorage.setItem('radiotedu_onair_station_id', String(state.stationId));
  localStorage.removeItem('deterministic_wall_station_id');
  $('stationSelect').innerHTML = state.stations.map((station) => `<option value="${Number(station.id)}">${escapeHtml(station.name)}</option>`).join('');
  $('stationSelect').value = String(state.stationId);
  $('stationCount').textContent = String(state.stations.length);
  document.querySelector('.station-card h2').textContent = state.stations.length ? 'Add another station' : 'Add your first station';
  $('workspaceStation').textContent = `Active: ${selectedStationName()}`;
}

async function loadCoreStatus() {
  const sid = state.stationId;
  const [health, runtime, ai, sweeper, dayparts, publicStations, stationSettings, stationOutput, libraryWatcher, productCatalog] = await Promise.all([
    api(`/api/health?station_id=${sid}`),
    api(`/api/runtime/${sid}/status`),
    api(`/api/ai/settings?station_id=${sid}`),
    api(`/api/sweeper/config?station_id=${sid}`),
    api(`/api/dayparts?station_id=${sid}`).catch(() => null),
    rawFetch('/api/public/stations', { cache: 'no-store' }, 12000).then((response) => response.ok ? response.json() : { stations: [] }),
    api(`/api/settings/station?station_id=${sid}`),
    api(`/api/stations/output?station_id=${sid}`),
    api('/api/library/watcher/status').catch(() => ({ running: false, profiles: [] })),
    IS_RTAI_ONAIR
      ? Promise.resolve({ running: false, products: [] })
      : api('/api/library/product-catalog/status').catch(() => ({ running: false, products: [] })),
  ]);
  state.health = health;
  state.runtime = runtime;
  state.timelineAnchorAt = Date.now();
  state.ai = ai;
  state.sweeper = sweeper;
  state.dayparts = dayparts;
  state.stationSettings = stationSettings?.settings || stationSettings || {};
  state.stationOutput = stationOutput || {};
  state.libraryWatcher = libraryWatcher || { running: false, profiles: [] };
  state.productCatalog = productCatalog || { running: false, products: [] };
  const publicStation = (publicStations.stations || []).find((station) => Number(station.id) === Number(sid));
  state.publicStation = publicStation || null;
  renderCoreStatus(publicStation);
  renderLibraryProfile();
  renderUnifiedMedia();
  renderProductCatalog();
  renderOutputConfiguration();
  renderAiConfiguration();
  renderDayparts();
  renderTimeline();
  renderEmergencyStatus(runtime);
}

async function loadOperatorConfiguration() {
  const [setupState, devicePayload, campaign, integrations, radioteduServices, unifiedMedia, watchdog] = await Promise.all([
    api(`/api/setup/state?station_id=${state.stationId}`),
    api('/api/audio/devices').catch(() => ({ devices: [] })),
    IS_RTAI_ONAIR
      ? Promise.resolve({ configured: false, active: false, stations: [] })
      : api('/api/campaign').catch(() => ({ configured: false, active: false, stations: [] })),
    IS_RTAI_ONAIR
      ? Promise.resolve({ voting_enabled: false, study_enabled: false })
      : api('/api/integrations/radiotedu').catch(() => ({
        voting_enabled: false,
        study_enabled: false,
      })),
    IS_RTAI_ONAIR
      ? Promise.resolve({ services: {}, definitions: [], status: [] })
      : state.radioteduServices
        ? Promise.resolve(state.radioteduServices)
        : api('/api/integrations/radiotedu/services?refresh_health=false').catch(() => ({
          services: {},
          definitions: [],
          status: [],
        })),
    // This endpoint summarizes large media views. Load it on explicit
    // configuration refreshes, never from the five-second live status poll.
    IS_RTAI_ONAIR
      ? Promise.resolve({ root: '', views: [], source_map_configured: false, last_error: '' })
      : api('/api/library/unified-media/status').catch(() => ({ root: '', views: [], source_map_configured: false, last_error: '' })),
    api('/api/watchdog/status').catch(() => null),
  ]);
  state.setupState = setupState || {};
  state.audioDevices = Array.isArray(devicePayload?.devices) ? devicePayload.devices : [];
  state.campaign = campaign || { configured: false, active: false, stations: [] };
  state.integrations = integrations || {};
  state.radioteduServices = radioteduServices || { services: {}, definitions: [], status: [] };
  state.unifiedMedia = unifiedMedia || { root: '', views: [], source_map_configured: false, last_error: '' };
  state.watchdog = watchdog;
  renderOutputConfiguration();
  renderAiConfiguration();
  renderCampaign();
  renderIntegrations();
  renderRadioTEDUServices();
  renderReadiness();
  renderWatchdog();
}

async function loadSelectedStationOutput() {
  const sid = Number(state.stationId);
  const [stationSettings, stationOutput] = await Promise.all([
    api(`/api/settings/station?station_id=${sid}`),
    api(`/api/stations/output?station_id=${sid}`),
  ]);
  // Ignore a late response from the station that was selected previously.
  if (sid !== Number(state.stationId)) return;
  state.stationSettings = stationSettings?.settings || stationSettings || {};
  state.stationOutput = stationOutput || {};
  renderOutputConfiguration();
  renderLibraryProfile();
}

function renderWatchdog() {
  const watchdog = state.watchdog;
  if (!watchdog) {
    $('watchdogState').textContent = 'Unavailable';
    $('watchdogSummary').textContent = 'Watchdog status could not be loaded.';
    return;
  }
  const stations = Array.isArray(watchdog.stations) ? watchdog.stations : [];
  const healthyRuntime = stations.filter((item) => {
    const runtime = item.runtime || {};
    return Boolean(runtime.running && runtime.worker_running && runtime.program_running && runtime.input_present && runtime.output_running)
      && runtime.mount_healthy !== false;
  }).length;
  const profilesOk = Boolean(watchdog.managed_profiles_ok);
  const lastRun = watchdog.last_run || {};
  const lastStatus = String(lastRun.status || 'not reported');
  $('watchdogState').textContent = healthyRuntime === stations.length && profilesOk ? 'Healthy' : 'Attention';
  $('watchdogSummary').textContent = `${healthyRuntime}/${stations.length || 6} station runtimes healthy · managed H: profiles ${profilesOk ? 'healthy' : 'need repair'} · last task: ${lastStatus}`;
}

function localDateTimeValue(value, fallbackDate) {
  const date = value ? new Date(value) : fallbackDate;
  if (!(date instanceof Date) || Number.isNaN(date.getTime())) return '';
  const pad = (part) => String(part).padStart(2, '0');
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function renderCampaign() {
  const campaign = state.campaign || {};
  const startsFallback = new Date();
  const endsFallback = new Date(startsFallback.getTime() + (30 * 24 * 60 * 60 * 1000));
  setCleanValue('campaignName', campaign.name || 'RadioTEDU No-Copyright Month');
  setCleanValue('campaignStartsAt', localDateTimeValue(campaign.starts_at, startsFallback));
  setCleanValue('campaignEndsAt', localDateTimeValue(campaign.ends_at, endsFallback));
  setCleanChecked('campaignEnabled', campaign.configured ? Boolean(campaign.enabled) : true);
  setCleanChecked('campaignVotingEnabled', campaign.configured ? Boolean(campaign.voting_enabled) : true);
  setCleanChecked('campaignAiEnabled', campaign.configured ? Boolean(campaign.ai_enabled) : true);
  $('campaignState').textContent = campaign.configured ? String(campaign.state || 'configured') : 'Not configured';
  const stationText = (campaign.stations || [])
    .map((item) => `${String(item.genre || '').toUpperCase()}: ${Number(item.eligible_tracks || 0)} eligible`)
    .join(' · ');
  const round = campaign.round;
  const roundText = round
    ? ` Vote ${round.status}: ${Number(round.total_votes || 0)} total${round.winning_genre ? ` · winner ${round.winning_genre}` : ''}.`
    : '';
  $('campaignStationSummary').textContent = `${stationText || 'Save the campaign to verify eligible libraries.'}${roundText}`;
}

function renderIntegrations() {
  const config = state.integrations || {};
  setCleanChecked('votingEnabled', Boolean(config.voting_enabled));
  setCleanValue('votingBaseUrl', config.voting_base_url || '');
  setCleanValue('votingDeviceId', config.voting_agent_device_id || '');
  setCleanValue('votingAgentToken', '');
  setCleanChecked('studyEnabled', Boolean(config.study_enabled));
  setCleanValue('studyBaseUrl', config.study_base_url || '');
  $('integrationState').textContent = config.voting_enabled || config.study_enabled ? 'Configured' : 'Optional';
  $('votingAgentToken').placeholder = config.voting_agent_token_configured
    ? 'Saved securely — leave blank to keep'
    : 'Required when voting is enabled';
}

function serviceControlId(serviceId, field) {
  return `service-${serviceId}-${field}`;
}

function isWindowsScmOwned(definition, status = {}) {
  return String(status.startup_owner || definition.startup_owner || '') === 'windows_scm';
}

function windowsScmOwnershipText(autonomousStartup = {}) {
  const ready = autonomousStartup.ready === true;
  const state = String(autonomousStartup.state || (ready ? 'verified' : 'commissioning pending')).replaceAll('_', ' ');
  const reasons = Array.isArray(autonomousStartup.reasons) && autonomousStartup.reasons.length
    ? ` Blocking checks: ${autonomousStartup.reasons.map((reason) => escapeHtml(String(reason).replaceAll('_', ' '))).join(', ')}.`
    : '';
  const verifiedAt = autonomousStartup.verified_at
    ? ` Foreground evidence verified ${escapeHtml(new Date(autonomousStartup.verified_at).toLocaleString())}.`
    : '';
  const evidence = autonomousStartup.evidence && Object.keys(autonomousStartup.evidence).length
    ? ` Evidence: ${escapeHtml(JSON.stringify(autonomousStartup.evidence))}.`
    : '';
  return `<b>Windows SCM owns autonomous startup.</b> SCM enrollment is gated by commissioning; autonomous readiness: ${escapeHtml(state)}.${ready ? '' : reasons}${verifiedAt}${evidence} App startup is disabled here; Start/Stop remain manual controls.`;
}

function renderRadioTEDUServices() {
  const container = $('serviceControlCards');
  if (!container) return;
  const payload = state.radioteduServices || {};
  const definitions = Array.isArray(payload.definitions) ? payload.definitions : [];
  const signature = definitions.map((item) => `${item.id}:${item.startup_owner || ''}`).join('|');
  if (container.dataset.signature !== signature) {
    container.dataset.signature = signature;
    container.innerHTML = definitions.map((definition) => {
      const isOllama = definition.kind === 'ollama';
      const mounts = Array.isArray(definition.mounts) && definition.mounts.length
        ? ` Mounts: ${definition.mounts.join(', ')}.`
        : '';
      const windowsScmOwned = isWindowsScmOwned(definition);
      return `
        <section class="service-control-card" data-service-card="${escapeHtml(definition.id)}" data-state="disabled">
          <div class="service-card-head">
            <div>
              <div class="eyebrow">${escapeHtml(definition.product)}</div>
              <h4>${escapeHtml(definition.name)}</h4>
              <p>${escapeHtml(definition.description)}${escapeHtml(mounts)}</p>
            </div>
            <span class="mini-state service-card-state" id="${serviceControlId(definition.id, 'state')}">Loading</span>
          </div>
          <div class="service-switches">
            <label class="check-row"><input id="${serviceControlId(definition.id, 'enabled')}" type="checkbox"> Enable management</label>
            <label class="check-row"><input id="${serviceControlId(definition.id, 'autostart')}" type="checkbox"${windowsScmOwned ? ' disabled' : ''}> Start with RadioTEDU OnAir</label>
          </div>
          <div class="service-startup-owner" id="${serviceControlId(definition.id, 'startup-owner')}"${windowsScmOwned ? '' : ' hidden'}>${windowsScmOwned ? '<b>Windows SCM owns autonomous startup.</b> SCM enrollment is gated by commissioning; Start/Stop remain manual controls.' : ''}</div>
          ${isOllama ? `
          <div class="ollama-controls">
            <div class="inline-status"><b>Local-only runtime</b><br>RadioTEDU OnAir detects the installed Ollama executable and talks only to 127.0.0.1. AI can be disabled without affecting music, microphone, or streaming.</div>
            <label>Model to install<input id="${serviceControlId(definition.id, 'model')}" value="qwen2.5:0.5b" maxlength="120" autocomplete="off" placeholder="qwen2.5:0.5b"></label>
          </div>` : `<div class="service-paths">
            <div class="service-path-picker">
              <label>Component source folder<input id="${serviceControlId(definition.id, 'source')}" autocomplete="off" placeholder="Absolute local source path"></label>
              <button class="button secondary" type="button" data-service-path="source" data-service-id="${escapeHtml(definition.id)}" data-picker-kind="folder">Browse</button>
            </div>
            <div class="service-path-picker">
              <label>${definition.id.startsWith('rtai_') ? 'Protected configuration folder' : 'Protected .env file'}<input id="${serviceControlId(definition.id, 'config')}" autocomplete="off" placeholder="Absolute protected path"></label>
              <button class="button secondary" type="button" data-service-path="config" data-service-id="${escapeHtml(definition.id)}" data-picker-kind="${definition.id.startsWith('rtai_') ? 'folder' : 'file'}">Browse</button>
            </div>
            <label class="service-health-field">Health URLs — one per line<textarea id="${serviceControlId(definition.id, 'health')}" rows="2" placeholder="Loopback HTTP or external HTTPS"></textarea></label>
            ${definition.database_supported ? `<div class="service-backup-field service-path-picker"><label>Database backup folder<input id="${serviceControlId(definition.id, 'backup')}" autocomplete="off" placeholder="Required before database updates"></label><button class="button secondary" type="button" data-service-path="backup" data-service-id="${escapeHtml(definition.id)}" data-picker-kind="folder">Browse</button></div>` : ''}
          </div>`}
          <div class="service-health-summary" id="${serviceControlId(definition.id, 'summary')}">Save paths, then check health.</div>
          <div class="service-actions">
            <button class="button secondary" type="button" data-service-action="check" data-service-id="${escapeHtml(definition.id)}">Check</button>
            <button class="button secondary" type="button" data-service-action="start" data-service-id="${escapeHtml(definition.id)}">Start</button>
            <button class="button secondary" type="button" data-service-action="stop" data-service-id="${escapeHtml(definition.id)}">Stop</button>
            <button class="button secondary" type="button" data-service-action="restart" data-service-id="${escapeHtml(definition.id)}">Restart</button>
            ${isOllama ? `<button class="button secondary" type="button" data-service-action="pull_model" data-service-id="${escapeHtml(definition.id)}">Install model</button>` : `<button class="button secondary" type="button" data-service-action="update_repository" data-service-id="${escapeHtml(definition.id)}">Update repository</button>`}
            ${definition.database_supported ? `<button class="button danger" type="button" data-service-action="update_database" data-service-id="${escapeHtml(definition.id)}">Update database</button>` : ''}
          </div>
        </section>`;
    }).join('');
    container.querySelectorAll('input, textarea').forEach((node) => {
      node.addEventListener('input', () => { node.dataset.dirty = '1'; });
    });
  }
  const configurations = payload.services || {};
  const statuses = new Map((payload.status || []).map((item) => [item.id, item]));
  definitions.forEach((definition) => {
    const serviceId = definition.id;
    const config = configurations[serviceId] || {};
    const status = statuses.get(serviceId) || {};
    const windowsScmOwned = isWindowsScmOwned(definition, status);
    setCleanChecked(serviceControlId(serviceId, 'enabled'), Boolean(config.enabled));
    setCleanChecked(serviceControlId(serviceId, 'autostart'), Boolean(config.auto_start));
    const autoStart = $(serviceControlId(serviceId, 'autostart'));
    if (autoStart) {
      autoStart.disabled = windowsScmOwned;
      autoStart.title = windowsScmOwned
        ? 'Windows SCM owns autonomous startup. This saved RadioTEDU OnAir preference is retained but not used to start the service.'
        : '';
    }
    const startupOwner = $(serviceControlId(serviceId, 'startup-owner'));
    if (startupOwner) {
      startupOwner.hidden = !windowsScmOwned;
      startupOwner.innerHTML = windowsScmOwned
        ? windowsScmOwnershipText(status.autonomous_startup || {})
        : '';
    }
    if ($(serviceControlId(serviceId, 'source'))) setCleanValue(serviceControlId(serviceId, 'source'), config.source_dir || '');
    if ($(serviceControlId(serviceId, 'config'))) setCleanValue(serviceControlId(serviceId, 'config'), config.config_path || '');
    if ($(serviceControlId(serviceId, 'health'))) setCleanValue(serviceControlId(serviceId, 'health'), (config.health_urls || []).join('\n'));
    if ($(serviceControlId(serviceId, 'backup'))) {
      setCleanValue(serviceControlId(serviceId, 'backup'), config.database_backup_dir || '');
    }
    const stateLabel = String(status.state || (config.enabled ? 'configured' : 'disabled')).replaceAll('_', ' ');
    $(serviceControlId(serviceId, 'state')).textContent = stateLabel;
    const card = container.querySelector(`[data-service-card="${serviceId}"]`);
    if (card) card.dataset.state = status.state || 'disabled';
    const health = Array.isArray(status.health) ? status.health : [];
    const healthText = health.length
      ? health.map((item) => {
        const signals = item.signals && Object.keys(item.signals).length
          ? ` — ${escapeHtml(JSON.stringify(item.signals).slice(0, 360))}`
          : '';
        return `${item.ok ? 'OK' : 'FAIL'} ${escapeHtml(item.url)} (${escapeHtml(item.status || 'offline')}, ${escapeHtml(item.latency_ms)} ms)${signals}`;
      }).join('<br>')
      : 'Health has not been checked in this view.';
    const sourceText = definition.kind === 'ollama'
      ? status.source?.ready ? 'Ollama installed' : 'Ollama is not installed'
      : status.source?.ready
        ? `Source ready${status.source.commit ? ` at ${escapeHtml(status.source.commit)}${status.source.dirty ? ' (local changes)' : ''}` : ''}`
        : `Source ${status.source?.configured ? 'not ready' : 'not configured'}`;
    const mountText = Array.isArray(status.mounts) && status.mounts.length
      ? status.mounts.join(', ')
      : 'none';
    const database = status.database || {};
    const lastUpdate = database.last_update_at
      ? new Date(database.last_update_at).toLocaleString()
      : '';
    const databaseText = definition.database_supported
      ? `<br><b>Database: ${escapeHtml(database.kind || definition.database_kind || 'managed')}</b> · ${escapeHtml(String(database.state || 'not ready').replaceAll('_', ' '))} · Backups: ${database.backup_configured ? 'configured' : 'not configured'}${lastUpdate ? ` · Last update: ${escapeHtml(lastUpdate)} · ${Number((database.last_backup_files || []).length)} backup file(s)` : ''}`
      : '';
    const jukeSignals = serviceId === 'juke_media_agent'
      ? health.map((item) => item.signals || {}).find((signals) => signals.library_policy || signals.library_roots || signals.play_ledger)
      : null;
    const jukeRoots = Array.isArray(jukeSignals?.library_roots) ? jukeSignals.library_roots : [];
    const jukeLedger = jukeSignals?.play_ledger || null;
    const jukeCatalogText = jukeSignals
      ? `<br><b>Juke catalog: ${escapeHtml(jukeSignals.library_policy?.mode || 'unknown')}</b> · Rights filter: ${jukeSignals.library_policy?.rights_filter === false ? 'disabled' : 'unknown'} · ${jukeRoots.map((root) => `${escapeHtml(root.id || 'root')} ${Number(root.item_count || 0)}${root.ready ? '' : ' (not ready)'}`).join(' + ') || 'root counts unavailable'}`
      : '';
    const jukeLedgerText = jukeLedger
      ? `<br><b>Compliance ledger: ${jukeLedger.ok ? 'healthy' : 'needs attention'}</b> &middot; ${Number(jukeLedger.entries || 0)} permanent record(s) &middot; fsync ${jukeLedger.durable_fsync ? 'enabled' : 'not verified'} &middot; integrity ${jukeLedger.integrity_ok ? 'verified' : 'not verified'}`
      : '';
    $(serviceControlId(serviceId, 'summary')).innerHTML = `<b>${escapeHtml(sourceText)}</b> · Runtime: ${escapeHtml(status.runtime || 'stopped')} · Config: ${status.config_ready ? 'ready' : 'not ready'} · Mounts: ${escapeHtml(mountText)}<br>${healthText}${databaseText}${jukeCatalogText}${jukeLedgerText}`;
    const running = status.runtime === 'running';
    const externallyRunning = status.runtime === 'external';
    const start = card?.querySelector('[data-service-action="start"]');
    const stop = card?.querySelector('[data-service-action="stop"]');
    const restart = card?.querySelector('[data-service-action="restart"]');
    if (start) start.disabled = running || externallyRunning;
    if (stop) stop.disabled = !running;
    if (restart) restart.disabled = !running;
  });
  const active = (payload.status || []).filter((item) => item.runtime === 'running').length;
  const unhealthy = (payload.status || []).filter((item) => item.enabled && ['degraded', 'not_ready'].includes(item.state)).length;
  $('serviceControlState').textContent = unhealthy ? `${unhealthy} need attention` : active ? `${active} running` : 'Ready';
}

function renderJukeLibrary(payload = state.jukeLibrary || {}) {
  const roots = Array.isArray(payload.roots) ? payload.roots : [];
  const items = Array.isArray(payload.items) ? payload.items : [];
  const rootOptions = roots.map((root) => `<option value="${escapeHtml(root.id)}">${escapeHtml(root.label)}${root.ready ? '' : ' (not ready)'}</option>`).join('');
  const uploadRoot = $('jukeLibraryUploadRoot');
  const rootFilter = $('jukeLibraryRootFilter');
  const previousUploadRoot = uploadRoot.value;
  const previousFilter = rootFilter.value;
  uploadRoot.innerHTML = rootOptions || '<option value="">No configured library</option>';
  rootFilter.innerHTML = `<option value="">All configured libraries</option>${rootOptions}`;
  if (roots.some((root) => root.id === previousUploadRoot)) uploadRoot.value = previousUploadRoot;
  if (roots.some((root) => root.id === previousFilter)) rootFilter.value = previousFilter;
  const counts = Object.entries(payload.root_counts || {}).map(([id, count]) => `${id} ${Number(count || 0)}`).join(' Â· ');
  $('jukeLibraryState').textContent = payload.include_trash
    ? `${Number(payload.matched_count || 0)} retired`
    : `${Number(payload.matched_count || 0)} matched`;
  $('jukeLibraryState').title = counts || 'No library count available';
  $('jukeLibraryList').innerHTML = items.length ? items.map((item) => {
    const path = payload.include_trash ? item.original_relative_path : item.relative_path;
    const action = payload.include_trash ? 'restore' : 'retire';
    const actionPath = payload.include_trash ? item.trash_path : item.relative_path;
    return `<div class="record-row media-row">
      <div><b>${escapeHtml(item.name)}</b><small>${escapeHtml(item.root_id)} Â· ${escapeHtml(path || item.relative_path)} Â· ${formatBytes(item.size_bytes)}</small></div>
      <div class="row-actions"><button class="button ${action === 'retire' ? 'danger' : 'secondary'}" type="button" data-juke-library-action="${action}" data-juke-root="${escapeHtml(item.root_id)}" data-juke-path="${encodeURIComponent(actionPath || '')}">${action === 'retire' ? 'Retire safely' : 'Restore song'}</button></div>
    </div>`;
  }).join('') : '<div class="empty-state">No matching JukeLocal songs.</div>';
}

async function loadJukeLibrary({ busy = true } = {}) {
  if (busy) setBusy(true, 'Loading JukeLocal songsâ€¦', 'Reading configured libraries in stable path order');
  setResult('jukeLibraryResult');
  const params = new URLSearchParams({
    query: $('jukeLibrarySearch').value.trim(),
    root_id: $('jukeLibraryRootFilter').value,
    include_trash: $('jukeLibraryShowRetired').checked ? 'true' : 'false',
    limit: '300',
  });
  try {
    state.jukeLibrary = await api(`/api/integrations/radiotedu/juke-library?${params.toString()}`, { timeoutMs: 45000 });
    renderJukeLibrary();
    const message = `Verified: ${Number(state.jukeLibrary.returned_count || 0)} of ${Number(state.jukeLibrary.matched_count || 0)} matching JukeLocal song records loaded.`;
    setResult('jukeLibraryResult', message, 'success');
  } catch (error) {
    const message = errorMessage(error);
    setResult('jukeLibraryResult', message, 'error');
    $('jukeLibraryState').textContent = 'Unavailable';
    throw error;
  } finally {
    if (busy) setBusy(false);
  }
}

async function uploadJukeLibrarySongs(event) {
  event.preventDefault();
  const files = Array.from($('jukeLibraryFiles').files || []);
  if (!files.length) return setResult('jukeLibraryResult', 'Choose one or more audio files.', 'error');
  const form = new FormData();
  form.append('root_id', $('jukeLibraryUploadRoot').value);
  form.append('relative_folder', $('jukeLibraryFolder').value.trim());
  files.forEach((file) => form.append('files', file));
  setBusy(true, 'Uploading JukeLocal songsâ€¦', 'Writing files atomically and verifying the library');
  setResult('jukeLibraryResult');
  try {
    const result = await api('/api/integrations/radiotedu/juke-library/upload', {
      method: 'POST',
      body: form,
      timeoutMs: 120000,
    });
    $('jukeLibraryFiles').value = '';
    $('jukeLibrarySearch').value = '';
    $('jukeLibraryRootFilter').value = $('jukeLibraryUploadRoot').value;
    $('jukeLibraryShowRetired').checked = false;
    await loadJukeLibrary({ busy: false });
    const message = `Verified: ${Number(result.stored_count || 0)} JukeLocal song(s) stored${Number(result.failed_count || 0) ? `; ${Number(result.failed_count)} rejected` : ''}.`;
    setResult('jukeLibraryResult', message, Number(result.failed_count || 0) ? 'error' : 'success');
    logActivity(message, Number(result.failed_count || 0) ? 'error' : 'success');
    toast(message, Number(result.failed_count || 0) ? 'error' : 'success');
  } catch (error) {
    const message = errorMessage(error);
    setResult('jukeLibraryResult', message, 'error');
    logActivity(`JukeLocal upload failed: ${message}`, 'error');
    toast(message, 'error');
  } finally {
    setBusy(false);
  }
}

function clearJukeLibraryActionArms() {
  state.jukeLibraryActionArmed = {};
  document.querySelectorAll('[data-juke-library-action].armed').forEach((button) => {
    button.classList.remove('armed');
    button.textContent = button.dataset.originalLabel || button.textContent;
  });
}

async function controlJukeLibraryItem(button) {
  const action = button.dataset.jukeLibraryAction;
  const rootId = button.dataset.jukeRoot;
  const encodedPath = button.dataset.jukePath || '';
  const itemPath = decodeURIComponent(encodedPath);
  if (!['retire', 'restore'].includes(action) || !rootId || !itemPath) return;
  const key = `${action}:${rootId}:${encodedPath}`;
  const now = Date.now();
  if (!state.jukeLibraryActionArmed[key] || state.jukeLibraryActionArmed[key] < now) {
    clearJukeLibraryActionArms();
    state.jukeLibraryActionArmed[key] = now + 20000;
    button.dataset.originalLabel = button.textContent;
    button.classList.add('armed');
    button.textContent = action === 'retire' ? 'Confirm retire' : 'Confirm restore';
    setResult('jukeLibraryResult', 'Click the same button again within 20 seconds to confirm.', 'error');
    return;
  }
  setBusy(true, action === 'retire' ? 'Retiring JukeLocal songâ€¦' : 'Restoring JukeLocal songâ€¦', 'Using the recoverable library workflow');
  try {
    const body = action === 'retire'
      ? { root_id: rootId, relative_path: itemPath, confirmation: 'RETIRE JUKE SONG' }
      : { root_id: rootId, trash_path: itemPath, confirmation: 'RESTORE JUKE SONG' };
    const endpoint = action === 'retire'
      ? '/api/integrations/radiotedu/juke-library/retire'
      : '/api/integrations/radiotedu/juke-library/restore';
    await api(endpoint, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
      timeoutMs: 30000,
    });
    clearJukeLibraryActionArms();
    await loadJukeLibrary({ busy: false });
    const message = action === 'retire'
      ? 'Verified: the song was moved to recoverable JukeLocal storage.'
      : 'Verified: the JukeLocal song was restored to its original folder.';
    setResult('jukeLibraryResult', message, 'success');
    logActivity(message);
    toast(message);
  } catch (error) {
    clearJukeLibraryActionArms();
    const message = errorMessage(error);
    setResult('jukeLibraryResult', message, 'error');
    logActivity(`JukeLocal ${action} failed: ${message}`, 'error');
  } finally {
    setBusy(false);
  }
}

function collectRadioTEDUServiceSettings() {
  const payload = {};
  (state.radioteduServices?.definitions || []).forEach((definition) => {
    const serviceId = definition.id;
    const config = state.radioteduServices?.services?.[serviceId] || {};
    const status = (state.radioteduServices?.status || []).find((item) => item.id === serviceId) || {};
    const autoStart = $(serviceControlId(serviceId, 'autostart'));
    const windowsScmOwned = isWindowsScmOwned(definition, status);
    payload[serviceId] = {
      enabled: $(serviceControlId(serviceId, 'enabled')).checked,
      auto_start: windowsScmOwned ? Boolean(config.auto_start) : Boolean(autoStart?.checked),
      source_dir: $(serviceControlId(serviceId, 'source'))?.value.trim() || '',
      config_path: $(serviceControlId(serviceId, 'config'))?.value.trim() || '',
      health_urls: $(serviceControlId(serviceId, 'health'))?.value.split(/\r?\n/).map((value) => value.trim()).filter(Boolean) || config.health_urls || [],
      database_backup_dir: $(serviceControlId(serviceId, 'backup'))?.value.trim() || '',
    };
  });
  return payload;
}

async function pickRadioTEDUServicePath(button) {
  const serviceId = button.dataset.serviceId;
  const field = button.dataset.servicePath;
  const kind = button.dataset.pickerKind === 'file' ? 'file' : 'folder';
  const input = $(serviceControlId(serviceId, field));
  if (!serviceId || !field || !input) return;
  setBusy(true, `Selecting ${field.replaceAll('_', ' ')}…`, 'Use the operating-system picker');
  setResult('serviceControlResult');
  try {
    const result = await pickOperatorPath(
      kind,
      input.value.trim(),
      kind === 'file'
        ? `Select ${serviceId} protected configuration`
        : `Select ${serviceId} ${field.replaceAll('_', ' ')}`,
    );
    const selected = kind === 'file' ? result.path : result.folder;
    if (result.selected && selected) {
      input.value = selected;
      input.dataset.dirty = '1';
      const message = `${serviceId} ${field.replaceAll('_', ' ')} selected. Save settings to verify it.`;
      setResult('serviceControlResult', message, 'success');
      logActivity(message);
    }
  } catch (error) {
    const message = errorMessage(error);
    setResult('serviceControlResult', message, 'error');
    logActivity(`Path selection failed: ${message}`, 'error');
  } finally {
    setBusy(false);
  }
}

function isBroadcastVerifiedLive(publicStation = state.publicStation) {
  return String(publicStation?.status || '').toLowerCase() === 'live';
}

function renderCoreStatus(publicStation = state.publicStation) {
  const health = state.health || {};
  const runtime = health.runtime || state.runtime || {};
  const loop = health.worker_loop || state.runtime?.worker_loop || {};
  const branches = health.runtime_branch_health || runtime.branch_health || {};
  const deliveries = health.runtime_delivery_health || runtime.delivery_health || branches;
  const onAir = isBroadcastVerifiedLive(publicStation);
  $('broadcastTitle').textContent = onAir ? 'Broadcast is live' : 'Broadcast is stopped';
  $('onAirLamp').className = `status-lamp ${onAir ? 'live' : 'off'}`;
  $('onAirLamp').innerHTML = `<span></span><b>${onAir ? 'ON AIR' : 'OFF AIR'}</b>`;
  $('engineState').textContent = runtime.running || health.engine_running ? 'Running' : 'Stopped';
  $('loopState').textContent = loop.running ? 'Running' : 'Stopped';
  $('icecastState').textContent = deliveries.icecast ? 'Connected' : (health.output_mode === 'icecast' ? 'Disconnected' : 'Not selected');
  const recovery = runtime.recovery || state.runtime?.recovery || {};
  $('recoveryState').textContent = recovery.state === 'retry_wait'
    ? `Retry in ${Math.ceil(Number(recovery.retry_in_seconds || 0))}s`
    : (recovery.state === 'recovering' ? 'Recovering' : recovery.state === 'recovered' ? 'Recovered' : 'Idle');
  $('recoveryState').title = recovery.message || recovery.error_code || '';
  const nowPlaying = publicStation?.now_playing || {};
  const preservedItem = publicStation?.preserved_item || {};
  $('nowPlayingTitle').textContent = nowPlaying.title || preservedItem.title || 'No track reported';
  $('nowPlayingArtist').textContent = nowPlaying.artist || (preservedItem.title
    ? `Preserved at the front of the queue — ${preservedItem.artist || 'artist not provided'}`
    : (onAir ? selectedStationName() : 'Broadcast is not running'));

  const aiEnabled = asBool(state.ai?.ai_host_enabled);
  const readiness = health.ai_prefetch?.startup_state || {};
  $('aiTitle').textContent = aiEnabled ? 'AI host is enabled' : 'AI host is disabled';
  $('aiLamp').className = `status-lamp ${aiEnabled ? (readiness.ready ? 'live' : 'warming') : 'off'}`;
  $('aiLamp').innerHTML = `<span></span><b>${aiEnabled ? (readiness.ready ? 'ENABLED' : 'WARMING') : 'DISABLED'}</b>`;
  $('aiDescription').textContent = aiEnabled ? (readiness.message || 'AI intros are enabled for upcoming music.') : 'Tracks play without generated AI introductions.';
  $('aiProvider').textContent = state.ai?.tts_provider || '—';
  $('aiReadiness').textContent = aiEnabled ? (readiness.ready ? 'Ready' : readiness.state || 'Warming') : 'Disabled';
  setCleanChecked('sweeperEnabled', Boolean(state.sweeper?.enabled));
  $('sweeperLamp').textContent = state.sweeper?.enabled
    ? `On - every ${state.sweeper.interval} completed song${Number(state.sweeper.interval) === 1 ? '' : 's'}`
    : 'Off';
  setCleanValue('sweeperInterval', String(state.sweeper?.interval || 2));
  setCleanValue('sweeperMode', ['ordered', 'random'].includes(state.sweeper?.mode) ? state.sweeper.mode : 'ordered');
  setCleanChecked('broadcastAutostartEnabled', asBool(state.stationSettings?.broadcast_autostart_enabled));
  setCleanValue('autoplayShuffleSeed', deterministicRotationKey());
  $('deterministicPolicyState').textContent = String(state.stationSettings?.playback_selection_policy || 'stable_rotation') === 'stable_rotation'
    ? 'Stable rotation'
    : 'Save required';
  $('jingleCount').textContent = String(state.sweeper?.jingle_count || state.jingles.length || 0);
  syncActionButtons();
}

function renderLibraryProfile() {
  const folderInput = $('libraryFolder');
  if (!folderInput) return;
  const settings = state.stationSettings || {};
  if (folderInput.dataset.dirty !== '1') folderInput.value = settings.music_library_folder || '';
  if ($('libraryProfileLabel').dataset.dirty !== '1') $('libraryProfileLabel').value = settings.library_profile_label || '';
  if ($('libraryDefaultGenre').dataset.dirty !== '1') $('libraryDefaultGenre').value = settings.library_default_genre || '';
  if ($('libraryDefaultLanguage').dataset.dirty !== '1') $('libraryDefaultLanguage').value = settings.library_default_language || '';
  const managedMode = String(settings.library_management_mode || 'merge').toLowerCase();
  $('libraryReplaceOutside').checked = managedMode === 'replace';
  const label = String(settings.library_profile_label || '').trim();
  const folder = String(settings.music_library_folder || '').trim();
  const watcherProfile = (state.libraryWatcher?.profiles || []).find((profile) => (
    Number(profile.station_id) === Number(state.stationId) && profile.track_type === 'music'
  ));
  const watcherState = watcherProfile
    ? ` · auto ${watcherProfile.status || 'watching'}`
    : (folder ? ' · auto watcher pending' : '');
  const persistedActiveFiles = Number(settings.library_active_files);
  const activeFiles = Number.isFinite(persistedActiveFiles)
    ? persistedActiveFiles
    : 0;
  $('libraryProfileState').textContent = folder
    ? `${label || 'Managed folder'} · ${managedMode === 'replace' ? 'exact replacement' : 'merge'} · ${activeFiles} active items${watcherState}`
    : 'No managed music folder has been configured for this station.';
  $('libraryManagedPath').textContent = folder || 'Choose a folder below, then sync and verify it.';
  if ($('jingleFolder') && $('jingleFolder').dataset.dirty !== '1') {
    $('jingleFolder').value = settings.jingle_library_folder || '';
    $('jingleFolderReplace').checked = String(settings.jingle_library_management_mode || 'merge').toLowerCase() === 'replace';
  }
}

function formatBytes(value) {
  const bytes = Math.max(0, Number(value || 0));
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 ** 3) return `${(bytes / 1024 ** 2).toFixed(1)} MB`;
  return `${(bytes / 1024 ** 3).toFixed(1)} GB`;
}

function daypartRuleEditorHtml(rule, current) {
  const isCurrent = current
    && Number(current.day_of_week) === Number(rule.day_of_week)
    && Number(current.position) === Number(rule.position);
  return `<div class="daypart-rule ${isCurrent ? 'is-current' : ''}" data-daypart-rule data-daypart-day="${escapeHtml(rule.day || '')}">
    <input data-daypart-field="name" value="${escapeHtml(rule.name || 'New Program')}" aria-label="Program name">
    <span class="daypart-hours"><input data-daypart-field="start" type="time" value="${escapeHtml(rule.start || '00:00')}" aria-label="Start time"><span>to</span><input data-daypart-field="end" type="time" value="${escapeHtml(rule.end || '01:00')}" aria-label="End time"></span>
    <span class="daypart-bpm"><input data-daypart-field="min_bpm" type="number" min="30" max="240" step="1" value="${Number(rule.min_bpm || 80)}" aria-label="Minimum BPM"><span>to</span><input data-daypart-field="max_bpm" type="number" min="30" max="240" step="1" value="${Number(rule.max_bpm || 120)}" aria-label="Maximum BPM"></span>
    <button class="daypart-remove" type="button" data-daypart-remove>Remove</button>
  </div>`;
}

function renderDayparts() {
  const schedule = state.dayparts;
  if (!schedule) {
    $('daypartLamp').textContent = 'Unavailable';
    $('daypartCoverage').textContent = 'Dayparting status is unavailable on this backend.';
    $('daypartRules').innerHTML = '';
    return;
  }
  setCleanChecked('daypartEnabled', Boolean(schedule.enabled));
  setCleanValue('daypartTimezone', schedule.timezone || 'Europe/Istanbul');
  const current = schedule.current_program;
  $('daypartLamp').textContent = schedule.enabled
    ? (current ? `${current.day} · ${current.name} · ${current.min_bpm}–${current.max_bpm} BPM` : 'On · no matching block')
    : 'Off';
  const coverage = schedule.bpm_coverage || {};
  $('daypartCoverage').textContent = `BPM coverage: ${Number(coverage.coverage_percent || 0).toFixed(1)}% (${Number(coverage.bpm_known_tracks || 0)} of ${Number(coverage.total_music_tracks || 0)} music tracks). Unknown tracks remain eligible as a continuity fallback.`;
  $('daypartRules').innerHTML = (schedule.days || []).map((day) => `<section class="daypart-day-group" data-daypart-day-group="${escapeHtml(day.day)}">
    <div class="daypart-day-heading"><h3>${escapeHtml(day.day)}</h3><button class="button secondary compact" type="button" data-daypart-add="${escapeHtml(day.day)}">+ Add program</button></div>
    ${(day.rules || []).map((rule) => daypartRuleEditorHtml(rule, current)).join('')}
  </section>`).join('');
}

function daypartRulesFromForm() {
  return [...document.querySelectorAll('[data-daypart-rule]')].map((row) => ({
    day: row.dataset.daypartDay,
    name: row.querySelector('[data-daypart-field="name"]').value.trim(),
    start: row.querySelector('[data-daypart-field="start"]').value,
    end: row.querySelector('[data-daypart-field="end"]').value,
    min_bpm: Number(row.querySelector('[data-daypart-field="min_bpm"]').value),
    max_bpm: Number(row.querySelector('[data-daypart-field="max_bpm"]').value),
    enabled: true,
  }));
}

function daypartClockMinutes(value) {
  const [hour, minute] = String(value || '00:00').split(':').map(Number);
  return hour * 60 + minute;
}

function daypartMinuteClock(value) {
  const minute = ((Number(value) % 1440) + 1440) % 1440;
  return `${String(Math.floor(minute / 60)).padStart(2, '0')}:${String(minute % 60).padStart(2, '0')}`;
}

function addDaypartRule(day) {
  const group = [...document.querySelectorAll('[data-daypart-day-group]')]
    .find((candidate) => candidate.dataset.daypartDayGroup === day);
  const rows = group ? [...group.querySelectorAll('[data-daypart-rule]')] : [];
  if (!group || !rows.length) return;
  const target = rows.reduce((best, row) => {
    const start = daypartClockMinutes(row.querySelector('[data-daypart-field="start"]').value);
    const end = daypartClockMinutes(row.querySelector('[data-daypart-field="end"]').value);
    const duration = (end - start + 1440) % 1440;
    return !best || duration > best.duration ? { row, start, end, duration } : best;
  }, null);
  if (!target || target.duration < 2) return toast('No program block is large enough to split.', 'error');
  const split = (target.start + Math.floor(target.duration / 2)) % 1440;
  target.row.querySelector('[data-daypart-field="end"]').value = daypartMinuteClock(split);
  target.row.insertAdjacentHTML('afterend', daypartRuleEditorHtml({
    day,
    name: `New ${day} Program`,
    start: daypartMinuteClock(split),
    end: daypartMinuteClock(target.end),
    min_bpm: Number(target.row.querySelector('[data-daypart-field="min_bpm"]').value || 80),
    max_bpm: Number(target.row.querySelector('[data-daypart-field="max_bpm"]').value || 120),
  }, null));
}

function removeDaypartRule(row) {
  const group = row.closest('[data-daypart-day-group]');
  const rows = group ? [...group.querySelectorAll('[data-daypart-rule]')] : [];
  if (rows.length <= 1) return toast('Each day needs at least one program block.', 'error');
  const index = rows.indexOf(row);
  const previous = rows[(index - 1 + rows.length) % rows.length];
  previous.querySelector('[data-daypart-field="end"]').value = row.querySelector('[data-daypart-field="end"]').value;
  row.remove();
}

async function saveDayparts(event) {
  event.preventDefault();
  setResult('daypartResult');
  setBusy(true, 'Saving weekly schedule…', 'Validating all seven 24-hour program clocks');
  try {
    state.dayparts = await api(`/api/dayparts/${state.stationId}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ enabled: $('daypartEnabled').checked, timezone: $('daypartTimezone').value.trim(), rules: daypartRulesFromForm() }),
      idempotent: true,
    });
    renderDayparts();
    const message = `Verified: weekly daypart schedule saved for ${selectedStationName()}.`;
    setResult('daypartResult', message, 'success'); logActivity(message); toast(message);
  } catch (error) {
    const message = errorMessage(error); setResult('daypartResult', message, 'error'); toast(message, 'error');
  } finally {
    setBusy(false);
  }
}

async function resetDayparts() {
  setBusy(true, 'Restoring RadioTEDU schedule…', 'Loading the seven-day genre profile');
  try {
    state.dayparts = await api(`/api/dayparts/${state.stationId}/reset`, { method: 'POST', idempotent: true });
    renderDayparts();
    const message = `Verified: RadioTEDU weekly defaults restored for ${selectedStationName()}.`;
    setResult('daypartResult', message, 'success'); toast(message);
  } catch (error) {
    const message = errorMessage(error); setResult('daypartResult', message, 'error'); toast(message, 'error');
  } finally {
    setBusy(false);
  }
}

function setCleanValue(id, value) {
  const node = $(id);
  if (node && node.dataset.dirty !== '1') node.value = value ?? '';
}

function setCleanChecked(id, value) {
  const node = $(id);
  if (node && node.dataset.dirty !== '1') node.checked = Boolean(value);
}

function renderOutputConfiguration() {
  const output = state.stationOutput || {};
  const station = state.stations.find((item) => Number(item.id) === Number(state.stationId)) || {};
  setCleanValue('currentStationName', station.name || '');
  setCleanValue('currentOutputGain', Number(output.output_gain_db || 0));
  setCleanChecked('currentIcecastEnabled', output.icecast_enabled);
  setCleanValue('currentIcecastHost', output.icecast_host || '127.0.0.1');
  setCleanValue('currentIcecastPort', Number(output.icecast_port || 8000));
  setCleanValue('currentIcecastMount', output.icecast_mount || `/station${state.stationId || 1}`);
  setCleanValue('currentIcecastUser', output.icecast_user || 'source');
  setCleanValue('currentIcecastPassword', output.icecast_password || '');
  setCleanValue('currentIcecastProfile', /^(opus|he_aac|aac_low|aac_he_v2)_/.test(String(output.stream_codec_profile || '')) ? output.stream_codec_profile : 'aac_low_192');
  setCleanValue('currentSourceProtocol', output.source_protocol || 'icecast');
  setCleanChecked('currentIcecastTlsEnabled', asBool(output.icecast_tls_enabled));
  setCleanChecked('currentLocalEnabled', output.local_output_enabled);

  const selectedDevice = $('currentOutputDevice')?.dataset.dirty === '1'
    ? $('currentOutputDevice').value
    : String(output.output_device_id || '');
  const deviceOptions = [...state.audioDevices];
  if (selectedDevice && !deviceOptions.some((device) => String(device.id) === selectedDevice)) {
    deviceOptions.unshift({ id: selectedDevice, label: `${selectedDevice} (currently configured)` });
  }
  if ($('currentOutputDevice')) {
    $('currentOutputDevice').innerHTML = '<option value="">Windows system default output</option>' + deviceOptions.map((device) =>
      `<option value="${escapeHtml(device.id)}">${escapeHtml(device.label || device.id)}</option>`).join('');
    $('currentOutputDevice').value = selectedDevice;
  }
  const networkLabel = String(output.source_protocol || 'icecast').toUpperCase();
  const targets = [output.icecast_enabled ? `${networkLabel} ${output.icecast_mount || ''}`.trim() : '', output.local_output_enabled ? 'local monitor' : ''].filter(Boolean);
  $('outputConfigState').textContent = targets.length ? targets.join(' + ') : 'No output configured';
  const user = currentUser();
  const permissions = new Set(user.effective_permissions || []);
  const canUseAdvanced = ['admin', 'superadmin'].includes(String(user.role || '')) || permissions.has('stream.configure_advanced');
  $('streamAdvancedSettings').hidden = !canUseAdvanced;
  const destinationSelect = $('streamWizardDestination');
  if (destinationSelect) {
    const newOption = [...destinationSelect.options].find((option) => option.value === 'new');
    if (newOption) newOption.disabled = !canUseAdvanced;
    if (!canUseAdvanced) destinationSelect.value = 'saved';
  }
  ['currentIcecastHost', 'currentIcecastMount', 'currentIcecastUser', 'currentIcecastPassword'].forEach((id) => {
    if ($(id)) $(id).readOnly = !canUseAdvanced;
  });
  if ($('currentIcecastEnabled')) $('currentIcecastEnabled').disabled = !canUseAdvanced;
  if ($('currentSourceProtocol')) $('currentSourceProtocol').disabled = !canUseAdvanced;
  if ($('streamDestinationProtection')) $('streamDestinationProtection').hidden = canUseAdvanced;
  toggleCurrentOutputFields();
  renderStreamWizardSummary();
}

function renderAiConfiguration() {
  const settings = state.ai || {};
  setCleanChecked('aiConfigEnabled', asBool(settings.ai_host_enabled));
  setCleanValue('aiLlmModel', settings.llm_model || 'Qwen/Qwen2.5-0.5B-Instruct');
  setCleanValue('aiTtsProvider', settings.tts_provider || 'local-qwen-tts');
  setCleanValue('aiVoicePersona', settings.voice_persona || 'auto');
  setCleanValue('aiTtsModelPath', settings.tts_model_path || '');
  setCleanValue('aiMaxSeconds', Number(settings.announcement_max_seconds || 15));
  setCleanValue('aiStationInterval', Number(settings.station_id_announcement_interval || 1800));
  setCleanChecked('aiIncludeHistory', asBool(settings.include_music_history));
  setCleanChecked('aiEducational', asBool(settings.educational_segments_enabled));
  setCleanValue('aiPromptTemplate', settings.prompt_template || '');
  const enabled = asBool(settings.ai_host_enabled);
  $('aiConfigState').textContent = enabled ? `${settings.tts_provider || 'AI voice'} enabled` : 'Disabled; music continuity remains active';
  if (!state.busy) $('testAiButton').disabled = !enabled;
}

function renderReadiness() {
  const setup = state.setupState || {};
  const checks = Array.isArray(setup.checks) ? setup.checks : [];
  const node = $('readinessList');
  if (!node) return;
  node.innerHTML = checks.length ? checks.map((check) => {
    const optional = check.required === false && !check.ready;
    const className = check.ready ? 'ready' : (optional ? 'optional' : '');
    const label = optional
      ? `${check.label || check.name || 'Check'} · Optional`
      : (check.label || check.name || 'Check');
    return `<li class="${className}"><span><b>${escapeHtml(label)}</b>${escapeHtml(check.message || (check.ready ? 'Ready' : 'Needs attention'))}</span></li>`;
  }).join('') : '<li><span><b>Self-check unavailable</b>Run the check again when the backend is connected.</span></li>';
  const blocking = Array.isArray(setup.blocking_reasons)
    ? setup.blocking_reasons
    : (Array.isArray(setup.blocking) ? setup.blocking : []);
  $('readinessState').textContent = setup.can_complete ? 'Ready for broadcast' : `${blocking.length || checks.filter((check) => !check.ready).length} action(s) needed`;
}

function syncActionButtons() {
  if (state.busy) return;
  const runtime = state.health?.runtime || state.runtime || {};
  const loop = state.health?.worker_loop || state.runtime?.worker_loop || {};
  const running = Boolean(state.health?.engine_running && runtime.running && runtime.output_feed_active);
  const verifiedLive = isBroadcastVerifiedLive();
  const stationReady = Number(state.stationId || 0) > 0;
  $('startBroadcastButton').disabled = !stationReady || (verifiedLive && running && loop.running);
  $('startBroadcastButton').textContent = state.startArmedUntil > Date.now() ? 'Confirm start broadcast' : 'Start broadcast';
  $('stopBroadcastButton').disabled = !stationReady || (!running && !loop.running);
  $('stopBroadcastButton').textContent = state.stopArmedUntil > Date.now() ? 'Confirm stop — keep playlist' : 'Stop stream — keep playlist';
  const aiEnabled = asBool(state.ai?.ai_host_enabled);
  $('enableAiButton').disabled = aiEnabled;
  $('disableAiButton').disabled = !aiEnabled;
  if ($('testAiButton')) $('testAiButton').disabled = !aiEnabled;
  if ($('deleteStationButton')) $('deleteStationButton').disabled = state.stations.length <= 1;
  $('libraryPrev').disabled = state.libraryPage <= 1;
  $('libraryNext').disabled = state.libraryPage >= state.libraryPages;
}

function renderUnifiedMedia() {
  const payload = state.unifiedMedia || {};
  const root = String(payload.root || '').trim();
  const configured = Boolean(payload.source_map_configured);
  const lastError = String(payload.last_error || '').trim();
  const views = Array.isArray(payload.views) ? payload.views : [];
  const stateLabel = lastError
    ? 'Needs attention'
    : configured && payload.layout_ready
      ? 'Ready'
      : configured
        ? 'Layout pending'
        : 'Source map required';
  $('unifiedMediaState').textContent = stateLabel;
  $('unifiedMediaRoot').textContent = root || 'No media root is configured.';
  $('unifiedMediaViews').innerHTML = views.length
    ? views.map((view) => `<div class="unified-media-view ${view.exists ? 'ready' : 'pending'}"><b>${escapeHtml(view.directory || view.view || 'View')}</b><span>${Number(view.file_count || 0)} linked file(s) · ${view.exists ? 'available' : 'not published'}</span></div>`).join('')
    : '<div class="empty-state">No managed media views are available yet.</div>';
  const refreshAt = String(payload.last_refresh_at || payload.last_published_at || '').trim();
  const refreshText = refreshAt ? `Last refresh: ${new Date(refreshAt).toLocaleString()}` : 'No refresh has been recorded.';
  $('unifiedMediaDetails').textContent = lastError
    ? `${refreshText} Last error: ${lastError}`
    : `${refreshText} ${configured ? 'Source map is explicit and ready for a safe rebuild.' : 'Create the protected explicit source map before rebuilding.'}`;
  $('refreshUnifiedMediaButton').disabled = !configured;
}

function renderProductCatalog() {
  const container = $('productCatalogRows');
  if (!container) return;
  const payload = state.productCatalog || {};
  const products = Array.isArray(payload.products) ? payload.products : [];
  container.innerHTML = products.length
    ? products.map((product) => {
      const stateLabel = String(product.state || 'boot_reconcile').replaceAll('_', ' ');
      const error = String(product.error_code || '').trim();
      const detail = error
        ? `Error: ${error}`
        : `Generation ${Number(product.generation || 0)} · ${Number(product.file_count || 0)} audio file(s)`;
      return `<div class="product-catalog-row ${error ? 'needs-attention' : ''}"><div><b>${escapeHtml(product.directory || product.product)}</b><span>${escapeHtml(stateLabel)} · ${escapeHtml(detail)}</span></div><button class="button secondary compact" type="button" data-product-catalog-rescan="${escapeHtml(product.product)}">Rescan</button></div>`;
    }).join('')
    : '<div class="empty-state">Product catalog reconciliation is waiting for the fixed media folders.</div>';
}

async function requestProductCatalogRescan(product) {
  const requested = String(product || '').trim();
  if (!requested) return;
  setResult('unifiedMediaResult');
  try {
    const result = await api('/api/library/product-catalog/rescan', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ product: requested }), timeoutMs: 30000, idempotent: true,
    });
    state.productCatalog = result;
    renderProductCatalog();
    setResult('unifiedMediaResult', `${requested} catalog rescan queued; it will commit only after media is stable.`, 'success');
  } catch (error) {
    setResult('unifiedMediaResult', errorMessage(error), 'error');
  }
}

function disarmStartBroadcast() {
  state.startArmedUntil = 0;
  if (state.startArmTimer) window.clearTimeout(state.startArmTimer);
  state.startArmTimer = null;
  $('startBroadcastButton').textContent = 'Start broadcast';
}

function disarmStopBroadcast() {
  state.stopArmedUntil = 0;
  if (state.stopArmTimer) window.clearTimeout(state.stopArmTimer);
  state.stopArmTimer = null;
  $('stopBroadcastButton').textContent = 'Stop stream — keep playlist';
}

async function loadQueue() {
  const payload = await api(`/api/queue?station_id=${state.stationId}`);
  state.queue = Array.isArray(payload?.items) ? payload.items : [];
  state.queueRevision = String(payload?.revision || '');
  renderQueue();
  renderTimeline();
}

function renderQueue() {
  const node = $('queueList');
  if (!state.queue.length) {
    node.innerHTML = '<div class="empty-state">The queue is empty.</div>';
    return;
  }
  const movable = state.queue.filter((item) => {
    const played = Boolean(item.is_played) || String(item.status) === 'done';
    const current = Boolean(item.is_current) || String(item.status) === 'playing';
    return !played && !current && Number(item.queue_index ?? item.index ?? -1) >= 0;
  });
  const firstMovableIndex = Number(movable[0]?.queue_index ?? movable[0]?.index ?? -1);
  const lastMovableIndex = Number(movable[movable.length - 1]?.queue_index ?? movable[movable.length - 1]?.index ?? -1);
  node.innerHTML = state.queue.map((item) => {
    const queueIndex = Number(item.queue_index ?? item.index ?? -1);
    const played = Boolean(item.is_played) || String(item.status) === 'done';
    const current = Boolean(item.is_current) || String(item.status) === 'playing';
    const trackId = Number(item.track_id || 0);
    return `<div class="media-row ${current ? 'playing' : ''} ${played ? 'done' : ''}">
      <div class="media-title"><small>${current ? 'Playing now' : played ? 'Played' : item.is_next ? 'Up next' : 'Queued'}</small><b>${escapeHtml(item.title || 'Untitled')}</b><span>${escapeHtml(item.artist || item.track_type || '')}</span></div>
      <div class="row-actions">
        ${current ? `<button class="icon-button remove" data-queue-skip="${Number(item.id)}" title="Skip current audio" aria-label="Skip ${escapeHtml(item.title)}">Skip</button>` : (!played && queueIndex >= 0 ? `<button class="icon-button" data-queue-action="up" data-queue-item-id="${Number(item.id)}" title="Move up" aria-label="Move ${escapeHtml(item.title)} up" ${queueIndex === firstMovableIndex ? 'disabled' : ''}>↑</button><button class="icon-button" data-queue-action="down" data-queue-item-id="${Number(item.id)}" title="Move down" aria-label="Move ${escapeHtml(item.title)} down" ${queueIndex === lastMovableIndex ? 'disabled' : ''}>↓</button><button class="icon-button remove" data-queue-action="remove" data-queue-item-id="${Number(item.id)}" title="Remove" aria-label="Remove ${escapeHtml(item.title)}">×</button>` : '')}
      </div><input type="hidden" value="${trackId}">
    </div>`;
  }).join('');
}

async function loadScheduleItems() {
  const payload = await api(`/api/schedule/items?station_id=${state.stationId}&limit=50`);
  state.scheduleItems = Array.isArray(payload?.items) ? payload.items : [];
  const node = $('scheduleItems');
  $('scheduleCount').textContent = String(state.scheduleItems.length);
  node.innerHTML = state.scheduleItems.length ? state.scheduleItems.map((item) => `<div class="media-row">
    <div class="media-title"><small>${escapeHtml(item.status || 'pending')} · track ${Number(item.track_id)}</small><b>${escapeHtml(item.title || 'Scheduled track')}</b><span>${escapeHtml(item.artist || '')} · ${escapeHtml(new Date(item.play_at).toLocaleString())}${item.window_end ? ` → ${escapeHtml(new Date(item.window_end).toLocaleString())}` : ''}</span></div>
  </div>`).join('') : '<div class="empty-state">No scheduled items for this station.</div>';
}

async function createScheduleItem(event) {
  event.preventDefault();
  const playAt = new Date($('schedulePlayAt').value);
  const windowValue = $('scheduleWindowEnd').value;
  const windowEnd = windowValue ? new Date(windowValue) : null;
  if (!Number.isFinite(playAt.getTime())) return setResult('scheduleResult', 'Choose a valid playout time.', 'error');
  if (windowEnd && (!Number.isFinite(windowEnd.getTime()) || windowEnd <= playAt)) return setResult('scheduleResult', 'Window end must be after the playout time.', 'error');
  setBusy(true, 'Scheduling track…', 'Persisting an exact-time station event');
  setResult('scheduleResult');
  try {
    await api('/api/schedule/items', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ station_id: state.stationId, track_id: Number($('scheduleTrackId').value), play_at: playAt.toISOString(), window_end: windowEnd ? windowEnd.toISOString() : null }),
      idempotent: true,
    });
    await loadScheduleItems();
    setResult('scheduleResult', 'Verified: scheduled item persisted for this station.', 'success');
    logActivity(`Scheduled track ${Number($('scheduleTrackId').value)} for ${selectedStationName()}.`);
  } catch (error) { setResult('scheduleResult', errorMessage(error), 'error'); }
  finally { setBusy(false); }
}

async function loadRecoveryPoints() {
  const payload = await api('/api/recovery/points?limit=50');
  state.recoveryPoints = Array.isArray(payload?.points) ? payload.points : [];
  $('recoveryCount').textContent = String(state.recoveryPoints.length);
  $('recoveryPoints').innerHTML = state.recoveryPoints.length ? state.recoveryPoints.map((point) => `<div class="media-row">
    <div class="media-title"><small>${escapeHtml(point.tier)} · ${escapeHtml(point.integrity_status)}</small><b>${escapeHtml(point.file_name)}</b><span>${formatBytes(point.size_bytes)} · ${escapeHtml(new Date(point.created_at).toLocaleString())}</span></div>
    <div class="row-actions"><button class="button secondary compact" type="button" data-recovery-verify="${Number(point.id)}">Verify restore</button></div>
  </div>`).join('') : '<div class="empty-state">No recovery points have been recorded.</div>';
}

function operatorIsAdministrator() {
  return ['admin', 'superadmin'].includes(String(currentUser().role || '').toLowerCase());
}

function renderDiagnosticBundles() {
  const allowed = operatorIsAdministrator();
  $('createDiagnosticBundleButton').hidden = !allowed;
  $('refreshDiagnosticBundlesButton').hidden = !allowed;
  $('diagnosticBundleCount').textContent = String(state.diagnosticBundles.length);
  if (!allowed) {
    $('diagnosticBundleList').innerHTML = '<div class="empty-state">Administrator access is required to create or download support evidence.</div>';
    return;
  }
  $('diagnosticBundleList').innerHTML = state.diagnosticBundles.length
    ? state.diagnosticBundles.map((bundle) => `<div class="media-row">
      <div><b>${escapeHtml(bundle.name)}</b><small>${escapeHtml(bundle.created_at || '')} · ${formatBytes(bundle.size_bytes || 0)}</small></div>
      <div class="row-actions"><button class="button secondary compact" type="button" data-diagnostic-download="${escapeHtml(bundle.name)}">Download</button></div>
    </div>`).join('')
    : '<div class="empty-state">No redacted diagnostic bundle has been created.</div>';
}

async function loadDiagnosticBundles() {
  if (!operatorIsAdministrator()) {
    state.diagnosticBundles = [];
    renderDiagnosticBundles();
    return;
  }
  const payload = await api('/api/recovery/diagnostics');
  state.diagnosticBundles = Array.isArray(payload?.bundles) ? payload.bundles : [];
  renderDiagnosticBundles();
}

async function createDiagnosticBundle() {
  setBusy(true, 'Creating redacted support bundle…', 'Collecting bounded health, integrity, and log-tail evidence');
  setResult('diagnosticBundleResult');
  try {
    const result = await api('/api/recovery/diagnostics', {
      method: 'POST', idempotent: true, timeoutMs: 120000,
    });
    await loadDiagnosticBundles();
    setResult('diagnosticBundleResult', `Created ${result.name} (${formatBytes(result.size_bytes || 0)}).`, 'success');
    logActivity('Created a redacted diagnostic support bundle.');
  } catch (error) {
    setResult('diagnosticBundleResult', errorMessage(error), 'error');
  } finally { setBusy(false); }
}

async function downloadDiagnosticBundle(name, retry = true) {
  const safeName = String(name || '');
  const headers = new Headers();
  const token = localStorage.getItem(AUTH_KEYS.access);
  if (token) headers.set('Authorization', `Bearer ${token}`);
  const response = await rawFetch(`/api/recovery/diagnostics/${encodeURIComponent(safeName)}`, { headers }, 120000);
  if (response.status === 401 && retry && await refreshSession()) return downloadDiagnosticBundle(safeName, false);
  if (!response.ok) throw parseResponseError(await response.text(), response.status, response.headers.get('X-Request-ID') || '');
  const objectUrl = URL.createObjectURL(await response.blob());
  try {
    const link = document.createElement('a');
    link.href = objectUrl;
    link.download = safeName;
    link.click();
  } finally {
    window.setTimeout(() => URL.revokeObjectURL(objectUrl), 1000);
  }
}

async function createRecoveryPoint(event) {
  event.preventDefault();
  setBusy(true, 'Creating recovery point…', 'Backing up, protecting, and opening a temporary restore');
  setResult('recoveryResult');
  try {
    const result = await api('/api/recovery/points', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ tier: $('recoveryTier').value }), idempotent: true, timeoutMs: 120000,
    });
    if (!result?.verified) throw new Error('Recovery point was not verified');
    await loadRecoveryPoints();
    setResult('recoveryResult', `Verified recovery point created: ${result.file_name}.`, 'success');
    logActivity(`Created and verified a ${result.tier} recovery point.`);
  } catch (error) { setResult('recoveryResult', errorMessage(error), 'error'); }
  finally { setBusy(false); }
}

async function verifyRecoveryPoint(pointId) {
  setBusy(true, 'Verifying recovery point…', 'Checking its hash and opening an isolated restore');
  setResult('recoveryResult');
  try {
    const result = await api(`/api/recovery/points/${Number(pointId)}/verify`, { method: 'POST', idempotent: true, timeoutMs: 120000 });
    if (!result?.valid) throw new Error('Recovery point failed verification and must not be restored');
    await loadRecoveryPoints();
    setResult('recoveryResult', 'Verified: the selected point is restorable.', 'success');
  } catch (error) { setResult('recoveryResult', errorMessage(error), 'error'); }
  finally { setBusy(false); }
}

function timelineSnapshot(now = new Date()) {
  const activeItems = state.queue.filter((item) => !item.is_played && String(item.status || '').toLowerCase() !== 'done');
  const reportedCurrent = activeItems.find((item) => item.is_current || String(item.status || '').toLowerCase() === 'playing') || null;
  const current = isBroadcastVerifiedLive() ? reportedCurrent : null;
  const pending = activeItems.filter((item) => item !== current);
  const duration = Math.max(0, Number(current?.duration || 0));
  const anchorAge = state.timelineAnchorAt ? Math.max(0, (Date.now() - state.timelineAnchorAt) / 1000) : 0;
  const elapsed = current ? Math.max(0, Number(state.runtime?.elapsed || 0) + anchorAge) : 0;
  const remaining = duration > 0 ? Math.max(0, duration - elapsed) : null;
  const startedAt = current ? new Date(now.getTime() - elapsed * 1000) : null;
  const endsAt = remaining === null ? null : new Date(now.getTime() + remaining * 1000);
  let cursor = endsAt || now;
  const forecast = pending.slice(0, 10).map((item) => {
    const backendStart = estimatedClockDate(item.estimated_time, now);
    const start = backendStart && backendStart.getTime() >= now.getTime() - 5000 ? backendStart : new Date(cursor);
    const itemDuration = Math.max(0, Number(item.duration || 0));
    const end = itemDuration > 0 ? new Date(start.getTime() + itemDuration * 1000) : null;
    cursor = end || start;
    return { item, start, end, duration: itemDuration };
  });
  return { current, duration, elapsed, remaining, startedAt, endsAt, forecast };
}

function renderTimeline() {
  const now = new Date();
  const timeline = timelineSnapshot(now);
  $('timelineClock').textContent = formatClock(now);
  if (!timeline.current) {
    const preservedItem = state.publicStation?.preserved_item || {};
    $('timelineNowTitle').textContent = preservedItem.title ? `Preserved: ${preservedItem.title}` : 'No track is currently playing';
    $('timelineNowArtist').textContent = preservedItem.title
      ? `${preservedItem.artist || 'Artist not provided'} — broadcast is not verified live`
      : 'Broadcast is stopped';
    $('timelineRemaining').textContent = '--:--';
    $('timelineEndTime').textContent = 'End time unavailable';
    $('timelineProgressBar').style.width = '0%';
  } else {
    $('timelineNowTitle').textContent = timeline.current.title || 'Untitled';
    $('timelineNowArtist').textContent = timeline.current.artist || timeline.current.track_type || selectedStationName();
    $('timelineRemaining').textContent = timeline.remaining === null ? '--:--' : formatDuration(timeline.remaining);
    $('timelineEndTime').textContent = timeline.endsAt ? `Ends ${formatClock(timeline.endsAt)}` : 'Duration unavailable';
    const progress = timeline.duration > 0 ? Math.max(0, Math.min(100, (timeline.elapsed / timeline.duration) * 100)) : 0;
    $('timelineProgressBar').style.width = `${progress.toFixed(2)}%`;
  }
  $('forecastList').innerHTML = timeline.forecast.length ? timeline.forecast.map(({ item, start, end, duration }, index) => `
    <li class="forecast-item">
      <span class="forecast-index">${String(index + 1).padStart(2, '0')}</span>
      <span class="forecast-track"><b>${escapeHtml(item.title || 'Untitled')}</b><span>${escapeHtml(item.artist || item.track_type || '')}${duration > 0 ? ` · ${formatDuration(duration)}` : ''}</span></span>
      <span class="forecast-time"><b>${formatClock(start)}</b><small>${end ? `ends ${formatClock(end)}` : 'end unknown'}</small></span>
    </li>`).join('') : '<li class="empty-state">No upcoming songs are queued.</li>';
}

function startTimelineTimer() {
  stopTimelineTimer();
  renderTimeline();
  state.timelineTimer = window.setInterval(renderTimeline, 1000);
}

function stopTimelineTimer() {
  if (state.timelineTimer) window.clearInterval(state.timelineTimer);
  state.timelineTimer = null;
}

async function loadLibrary(page = state.libraryPage) {
  const search = encodeURIComponent($('librarySearch').value.trim());
  const type = encodeURIComponent($('libraryType').value);
  const payload = await api(`/api/tracks?station_id=${state.stationId}&page=${Math.max(1, page)}&per_page=12&search=${search}&track_type=${type}`);
  state.library = Array.isArray(payload?.tracks) ? payload.tracks : (payload?.items || []);
  state.libraryPage = Number(payload?.page || 1);
  state.libraryPages = Number(payload?.total_pages || 1);
  state.libraryTotal = Number(payload?.total || state.library.length);
  $('libraryCount').textContent = String(state.libraryTotal);
  $('libraryPage').textContent = `Page ${state.libraryPage} of ${state.libraryPages}`;
  renderLibrary();
  renderLibraryProfile();
  syncActionButtons();
}

function renderLibrary() {
  const node = $('libraryList');
  if (!state.library.length) {
    node.innerHTML = '<div class="empty-state">No matching audio found.</div>';
    return;
  }
  node.innerHTML = state.library.map((track) => `<div class="media-row">
    <div class="media-title"><small>${escapeHtml(track.track_type || 'music')}</small><b>${escapeHtml(track.title || 'Untitled')}</b><span>${escapeHtml(track.artist || track.album || 'Unknown artist')}</span></div>
    <div class="row-actions"><button class="icon-button add" data-add-track="${Number(track.id)}" title="Add to queue" aria-label="Add ${escapeHtml(track.title)} to queue">+ Queue</button></div>
  </div>`).join('');
}

async function loadJingles() {
  const payload = await api(`/api/tracks?station_id=${state.stationId}&track_type=jingle&per_page=50`);
  state.jingles = Array.isArray(payload?.tracks) ? payload.tracks : (payload?.items || []);
  $('jingleCount').textContent = String(state.jingles.length);
  $('jingleList').innerHTML = state.jingles.length ? state.jingles.map((track) => `<div class="media-row"><div class="media-title"><b>${escapeHtml(track.title || 'Untitled jingle')}</b><span>${escapeHtml(track.artist || 'Jingle')}</span></div><button class="icon-button add" data-add-track="${Number(track.id)}">+ Queue</button></div>`).join('') : '<div class="empty-state">No jingles loaded for this station.</div>';
}

async function syncLibraryFolder(event) {
  event.preventDefault();
  const folder = $('libraryFolder').value.trim();
  if (!folder) {
    setResult('librarySyncResult', 'Enter the full folder path that belongs to this station.', 'error');
    return;
  }
  const replace = $('libraryReplaceOutside').checked;
  const payload = {
    station_id: state.stationId,
    folder,
    recursive: $('libraryRecursive').checked,
    track_type: 'music',
    mode: replace ? 'replace' : 'merge',
    skip_unplayable: $('librarySkipUnplayable').checked,
    remove_pending_queue: replace,
    profile_label: $('libraryProfileLabel').value.trim(),
    default_genre: $('libraryDefaultGenre').value.trim(),
    default_language: $('libraryDefaultLanguage').value.trim(),
  };
  setBusy(true, 'Synchronizing station library…', replace
    ? 'Importing this folder, deactivating other music, rebuilding queue, and verifying'
    : 'Importing this folder and verifying every file');
  setResult('librarySyncResult');
  try {
    const result = await api('/api/library/folder/sync', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
      timeoutMs: 600000,
      idempotent: true,
      transportAttempts: 2,
    });
    if (!result?.verified) throw new Error('Backend did not verify the managed library');
    delete $('libraryFolder').dataset.dirty;
    delete $('libraryProfileLabel').dataset.dirty;
    delete $('libraryDefaultGenre').dataset.dirty;
    delete $('libraryDefaultLanguage').dataset.dirty;
    await Promise.all([loadCoreStatus(), loadQueue(), loadLibrary(1), loadJingles(), loadOperatorConfiguration(), loadScheduleItems(), loadRecoveryPoints()]);
    const skipped = Number(result.invalid_files_skipped || 0);
    const message = `Verified: ${result.active_files} ${payload.profile_label || payload.default_genre || 'music'} file(s) are active for ${selectedStationName()}; ${result.added} added, ${result.deactivated} outside the folder deactivated, ${result.pending_queue_items_removed} stale queue item(s) removed${skipped ? `, ${skipped} unreadable file(s) skipped and reported` : ''}.`;
    setResult('librarySyncResult', message, 'success');
    logActivity(message);
    toast('Station library synchronized and verified');
  } catch (error) {
    const message = errorMessage(error);
    setResult('librarySyncResult', message, 'error');
    logActivity(`Library synchronization failed: ${message}`, 'error');
    toast(message, 'error');
  } finally {
    setBusy(false);
  }
}

async function requestManagedLibraryRescan() {
  setBusy(true, 'Queuing managed-folder rescan…', 'The watcher waits for files to finish copying before validation');
  setResult('librarySyncResult');
  try {
    const result = await api('/api/library/watcher/rescan', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ station_id: state.stationId, track_type: 'music' }),
      idempotent: true,
    });
    state.libraryWatcher = result;
    const queued = Number(result?.queued_profiles || 0);
    const message = queued
      ? 'Managed-folder rescan queued. New or changed files will be validated after they are stable.'
      : 'No managed music folder is active for this station. Save and verify a folder first.';
    setResult('librarySyncResult', message, queued ? 'success' : 'error');
    renderLibraryProfile();
  } catch (error) {
    const message = errorMessage(error);
    setResult('librarySyncResult', message, 'error');
    logActivity(`Managed-folder rescan failed: ${message}`, 'error');
  } finally {
    setBusy(false);
  }
}

async function refreshUnifiedMedia() {
  setBusy(true, 'Refreshing unified media views…', 'Replacing mapped links, preserving verified drop-ins, and queuing the managed-library watcher');
  setResult('unifiedMediaResult');
  try {
    const result = await api('/api/library/unified-media/refresh', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ request_library_rescan: true }),
      timeoutMs: 600000,
      idempotent: true,
    });
    state.libraryWatcher = result.watcher || state.libraryWatcher;
    state.unifiedMedia = await api('/api/library/unified-media/status');
    renderUnifiedMedia();
    renderLibraryProfile();
    await Promise.all([loadLibrary(1), loadJingles()]);
    const total = Object.values(result.views || {}).reduce((sum, count) => sum + Number(count || 0), 0);
    const queued = Number(result.library_rescan_queued_profiles || 0);
    const message = `Verified: ${total} hardlink view file(s) refreshed while preserving verified operator drop-ins. ${queued} managed library profile(s) queued for safe rescan.`;
    setResult('unifiedMediaResult', message, 'success');
    logActivity(message);
    toast('Unified media views refreshed');
  } catch (error) {
    const message = errorMessage(error);
    setResult('unifiedMediaResult', message, 'error');
    logActivity(`Unified media refresh failed: ${message}`, 'error');
    toast(message, 'error');
    try {
      state.unifiedMedia = await api('/api/library/unified-media/status');
      renderUnifiedMedia();
    } catch (_) {
      // Preserve the original refresh failure for the operator.
    }
  } finally {
    setBusy(false);
  }
}

async function pickManagedFolder(inputId, description) {
  const input = $(inputId);
  setBusy(true, 'Choose a folder…', 'Use the native folder window, then return here');
  try {
    const result = await pickOperatorPath('folder', input.value.trim(), description);
    if (result?.selected && result.folder) {
      input.value = result.folder;
      input.dataset.dirty = '1';
      input.focus();
      toast('Folder selected');
    }
  } catch (error) {
    const message = errorMessage(error); toast(message, 'error'); logActivity(`Folder selection failed: ${message}`, 'error');
  } finally { setBusy(false); }
}

async function refreshAll(silent = false) {
  // Verified mutations call this before their busy overlay is released. Silent
  // refreshes must still replace station-scoped caches (library, queue,
  // jingles, and settings), otherwise a newly selected station can briefly
  // display the previous station's controls and media.
  if (!state.stationId || (state.busy && !silent)) return;
  if (!silent) setConnection('', 'Refreshing');
  try {
    const currentView = String(state.activeView || 'onair');
    const jobs = [loadCoreStatus(), loadQueue()];
    if (!IS_RTAI_ONAIR) jobs.push(loadHlsSettings());
    if (currentView === 'media') jobs.push(loadLibrary(1), loadJingles());
    if (['automation', 'stations', 'settings', 'diagnostics', 'services'].includes(currentView)) {
      jobs.push(loadOperatorConfiguration());
    }
    if (currentView === 'scheduler') jobs.push(loadScheduleItems());
    if (currentView === 'recovery') jobs.push(loadRecoveryPoints());
    await Promise.all(jobs);
    setConnection('online', 'Backend connected');
  } catch (error) {
    setConnection('offline', 'Connection failed');
    if (!silent) toast(errorMessage(error), 'error');
  }
}

async function saveBroadcastAutostart(enabled) {
  const payload = await api('/api/settings/station', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      station_id: state.stationId,
      broadcast_autostart_enabled: enabled ? 'true' : 'false',
      playback_selection_policy: 'stable_rotation',
      autoplay_shuffle_seed: deterministicRotationKey(),
    }),
    idempotent: true,
  });
  const saved = payload?.settings || payload || {};
  if (asBool(saved.broadcast_autostart_enabled) !== Boolean(enabled)) {
    const readBack = await api(`/api/settings/station?station_id=${state.stationId}`);
    const settings = readBack?.settings || readBack || {};
    if (asBool(settings.broadcast_autostart_enabled) !== Boolean(enabled)) {
      throw new Error('Backend did not persist the station broadcast start policy');
    }
  }
}

async function saveDeterministicPolicy() {
  const seed = $('autoplayShuffleSeed').value.trim();
  if (seed.length < 3 || seed.length > 120 || /[\u0000-\u001f]/u.test(seed)) {
    setResult('broadcastResult', 'Enter a rotation key from 3 to 120 printable characters.', 'error');
    return;
  }
  setBusy(true, 'Saving deterministic policyâ€¦', 'Persisting and verifying stable playback selection');
  setResult('broadcastResult');
  try {
    await api('/api/settings/station', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        station_id: state.stationId,
        autoplay_shuffle_seed: seed,
        playback_selection_policy: 'stable_rotation',
      }),
      idempotent: true,
    });
    const readBack = await api(`/api/settings/station?station_id=${state.stationId}`);
    const settings = readBack?.settings || readBack || {};
    if (String(settings.autoplay_shuffle_seed || '') !== seed || String(settings.playback_selection_policy || '') !== 'stable_rotation') {
      throw new Error('Backend did not persist the deterministic playback policy');
    }
    state.stationSettings = settings;
    clearFormDirty(['autoplayShuffleSeed']);
    renderCoreStatus(state.publicStation);
    const message = `Verified: ${selectedStationName()} uses the saved stable rotation key.`;
    setResult('broadcastResult', message, 'success');
    logActivity(message);
    toast('Deterministic policy verified');
  } catch (error) {
    const message = errorMessage(error);
    setResult('broadcastResult', message, 'error');
    logActivity(`Deterministic policy failed: ${message}`, 'error');
    toast(message, 'error');
  } finally {
    setBusy(false);
  }
}

async function updateBroadcastAutostartFromControl() {
  const enabled = $('broadcastAutostartEnabled').checked;
  setBusy(true, 'Saving restart policy…', 'Persisting and verifying the selected station');
  setResult('broadcastResult');
  try {
    await saveBroadcastAutostart(enabled);
    clearFormDirty(['broadcastAutostartEnabled']);
    const message = enabled
      ? `Verified: ${selectedStationName()} will resume automatically when OnAir restarts.`
      : `Verified: ${selectedStationName()} will remain stopped when OnAir restarts.`;
    setResult('broadcastResult', message, 'success');
    logActivity(message);
    toast('Restart policy verified');
  } catch (error) {
    const message = errorMessage(error);
    setResult('broadcastResult', message, 'error');
    logActivity(`Restart policy failed: ${message}`, 'error');
    toast(message, 'error');
    await loadOperatorConfiguration().catch(() => {});
  } finally {
    setBusy(false);
  }
}

async function startBroadcast() {
  if (state.startArmedUntil <= Date.now()) {
    state.startArmedUntil = Date.now() + 20000;
    $('startBroadcastButton').textContent = 'Confirm start broadcast';
    setResult('broadcastResult', `Click “Confirm start broadcast” within 20 seconds to take ${selectedStationName()} on air.`);
    state.startArmTimer = window.setTimeout(() => {
      disarmStartBroadcast();
      setResult('broadcastResult', 'Start confirmation expired; nothing was changed.');
    }, 20000);
    return;
  }
  disarmStartBroadcast();
  disarmStopBroadcast();
  const resumeAfterRestart = $('broadcastAutostartEnabled').checked;
  setBusy(true, 'Starting broadcast…', 'Starting scheduler and verifying live output');
  setResult('broadcastResult');
  try {
    const current = await api(`/api/runtime/${state.stationId}/status`);
    const started = await verifiedMutation(async () => {
      await saveBroadcastAutostart(resumeAfterRestart);
      clearFormDirty(['broadcastAutostartEnabled']);
      if (!current?.worker_loop?.running) {
        await api(`/api/runtime/${state.stationId}/operator-start`, {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ fallback_uri: '', interval_sec: 1 }), idempotent: true, timeoutMs: 45000,
        });
      } else {
        await api(`/api/runtime/${state.stationId}/operator-supervise`, { method: 'POST', idempotent: true });
      }
    }, async () => {
      const [health, settingPayload, publicStations] = await Promise.all([
        api(`/api/health?station_id=${state.stationId}`),
        api(`/api/settings/station?station_id=${state.stationId}`),
        rawFetch('/api/public/stations', { cache: 'no-store' }, 12000)
          .then((response) => response.ok ? response.json() : { stations: [] }),
      ]);
      const settings = settingPayload?.settings || settingPayload || {};
      const runtime = health.runtime || {};
      const delivery = health.runtime_delivery_health || runtime.delivery_health || {};
      const publicStation = (publicStations.stations || [])
        .find((station) => Number(station.id) === Number(state.stationId)) || null;
      const verified = Boolean(
        asBool(settings.broadcast_autostart_enabled) === resumeAfterRestart
        && health.worker_loop?.running
        && health.engine_running
        && runtime.running
        && runtime.output_feed_active
        && (delivery.icecast || delivery.local)
        && isBroadcastVerifiedLive(publicStation)
      );
      return { verified, value: { health, publicStation } };
    }, { attempts: 50, interval: 500, description: 'broadcast output' });
    state.health = started.value.health;
    state.runtime = started.value.health?.runtime || state.runtime;
    state.publicStation = started.value.publicStation;
    renderCoreStatus(state.publicStation);
    setResult(
      'broadcastResult',
      `Verified end to end: scheduler, engine, output feed, and the public stream are live. Restart policy is ${resumeAfterRestart ? 'enabled' : 'disabled'}. Any preserved item resumes from the front without changing queue order.`,
      'success',
    );
    logActivity(`Started broadcasting ${selectedStationName()}`);
    toast('Broadcast start verified');
  } catch (error) {
    const message = errorMessage(error);
    setResult('broadcastResult', message, 'error');
    logActivity(`Broadcast start failed: ${message}`, 'error');
    toast(message, 'error');
  } finally {
    setBusy(false);
    await loadCoreStatus().catch(() => {});
  }
}

async function stopBroadcast() {
  if (state.stopArmedUntil <= Date.now()) {
    disarmStartBroadcast();
    state.stopArmedUntil = Date.now() + 20000;
    $('stopBroadcastButton').textContent = 'Confirm stop — keep playlist';
    setResult('broadcastResult', `Click “Confirm stop — keep playlist” within 20 seconds to stop ${selectedStationName()} without clearing or advancing its queue.`);
    state.stopArmTimer = window.setTimeout(() => {
      disarmStopBroadcast();
      setResult('broadcastResult', 'Stop confirmation expired; nothing was changed.');
    }, 20000);
    return;
  }
  disarmStopBroadcast();
  setBusy(true, 'Stopping stream…', 'Freezing scheduler state, preserving the playlist, and verifying silence');
  setResult('broadcastResult');
  try {
    const stopped = await verifiedMutation(async () => {
      await saveBroadcastAutostart(false);
      $('broadcastAutostartEnabled').checked = false;
      clearFormDirty(['broadcastAutostartEnabled']);
      return api(`/api/runtime/${state.stationId}/operator-stop`, { method: 'POST', idempotent: true });
    }, async () => {
      const [runtime, settingPayload] = await Promise.all([
        api(`/api/runtime/${state.stationId}/status`),
        api(`/api/settings/station?station_id=${state.stationId}`),
      ]);
      const settings = settingPayload?.settings || settingPayload || {};
      const verified = !asBool(settings.broadcast_autostart_enabled) && !runtime?.running && !runtime?.worker_loop?.running;
      if (verified) state.runtime = runtime;
      return { verified, value: runtime };
    }, { attempts: 30, interval: 350, description: 'stopped runtime and scheduler' });
    const stoppedRuntime = stopped.value;
    const preservation = stopped.mutationResult || {};
    state.runtime = stoppedRuntime;
    state.health = {
      ...(state.health || {}),
      engine_running: false,
      runtime_branch_health: { icecast: false, local: false },
      runtime: {
        ...((state.health || {}).runtime || {}),
        ...stoppedRuntime,
        running: false,
        output_feed_active: false,
      },
      worker_loop: {
        ...((state.health || {}).worker_loop || {}),
        ...(stoppedRuntime.worker_loop || {}),
        running: false,
      },
    };
    state.publicStation = {
      ...(state.publicStation || {}),
      status: 'offline',
      status_reason: 'Stopped by operator',
      preserved_item: state.publicStation?.now_playing || state.publicStation?.preserved_item || null,
      now_playing: null,
    };
    renderCoreStatus();
    setResult(
      'broadcastResult',
      `Verified: stream and scheduler are stopped; ${Number(preservation.queue_items_after || 0)} playlist item(s) remain in order. The interrupted item will restart from its beginning when you resume.`,
      'success',
    );
    logActivity(`Stopped broadcasting ${selectedStationName()}`);
    toast('Broadcast stop verified');
  } catch (error) {
    const message = errorMessage(error);
    setResult('broadcastResult', message, 'error');
    logActivity(`Broadcast stop failed: ${message}`, 'error');
    toast(message, 'error');
  } finally {
    setBusy(false);
    await loadCoreStatus().catch(() => {});
  }
}

async function setAiEnabled(enabled) {
  setBusy(true, enabled ? 'Enabling AI host…' : 'Disabling AI host…', 'Saving and reading the setting back');
  setResult('aiResult');
  try {
    const current = await api(`/api/ai/settings?station_id=${state.stationId}`);
    const payload = {
      station_id: state.stationId,
      ai_host_enabled: Boolean(enabled),
      llm_model: current.llm_model,
      tts_provider: current.tts_provider,
      tts_model_path: current.tts_model_path,
      voice_persona: current.voice_persona,
      announcement_max_seconds: Number(current.announcement_max_seconds || 15),
      include_music_history: asBool(current.include_music_history),
      educational_segments_enabled: asBool(current.educational_segments_enabled),
      station_id_announcement_interval: Number(current.station_id_announcement_interval || 1800),
      prompt_template: current.prompt_template,
    };
    const changed = await verifiedMutation(() => api('/api/ai/settings', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload),
      timeoutMs: 30000, idempotent: true,
    }), async () => {
      const saved = await api(`/api/ai/settings?station_id=${state.stationId}`);
      return { verified: asBool(saved.ai_host_enabled) === Boolean(enabled), value: saved };
    }, { attempts: 16, interval: 350, description: `AI ${enabled ? 'enabled' : 'disabled'} setting` });
    state.ai = changed.value;
    clearFormDirty(['aiConfigEnabled']);
    renderAiConfiguration();
    setResult('aiResult', `Verified: AI host is ${enabled ? 'enabled' : 'disabled'}.`, 'success');
    logActivity(`${enabled ? 'Enabled' : 'Disabled'} AI host for ${selectedStationName()}`);
    toast(`AI host ${enabled ? 'enabled' : 'disabled'}`);
  } catch (error) {
    const message = errorMessage(error);
    setResult('aiResult', message, 'error');
    logActivity(`AI change failed: ${message}`, 'error');
    toast(message, 'error');
  } finally {
    setBusy(false);
    await loadCoreStatus().catch(() => {});
  }
}

function aiPayloadFromForm() {
  const maxSeconds = Number($('aiMaxSeconds').value);
  const stationInterval = Number($('aiStationInterval').value);
  if (!Number.isFinite(maxSeconds) || maxSeconds < 3 || maxSeconds > 120) throw new Error('Maximum announcement must be between 3 and 120 seconds');
  if (!Number.isFinite(stationInterval) || stationInterval < 60 || stationInterval > 86400) throw new Error('Station ID interval must be between 60 and 86400 seconds');
  const llmModel = $('aiLlmModel').value.trim();
  const prompt = $('aiPromptTemplate').value.trim();
  if (!llmModel) throw new Error('Enter the language model name');
  if (!prompt) throw new Error('Enter an AI prompt template');
  return {
    station_id: state.stationId,
    ai_host_enabled: $('aiConfigEnabled').checked,
    llm_model: llmModel,
    tts_provider: $('aiTtsProvider').value,
    tts_model_path: $('aiTtsModelPath').value.trim(),
    voice_persona: $('aiVoicePersona').value,
    announcement_max_seconds: maxSeconds,
    include_music_history: $('aiIncludeHistory').checked,
    educational_segments_enabled: $('aiEducational').checked,
    station_id_announcement_interval: stationInterval,
    prompt_template: prompt,
  };
}

function aiSettingsMatch(saved, payload) {
  return Boolean(saved)
    && asBool(saved.ai_host_enabled) === payload.ai_host_enabled
    && String(saved.llm_model || '') === payload.llm_model
    && String(saved.tts_provider || '') === payload.tts_provider
    && String(saved.tts_model_path || '') === payload.tts_model_path
    && String(saved.voice_persona || '') === payload.voice_persona
    && Number(saved.announcement_max_seconds) === payload.announcement_max_seconds
    && asBool(saved.include_music_history) === payload.include_music_history
    && asBool(saved.educational_segments_enabled) === payload.educational_segments_enabled
    && Number(saved.station_id_announcement_interval) === payload.station_id_announcement_interval
    && String(saved.prompt_template || '') === payload.prompt_template;
}

async function persistAiPayload(payload) {
  const changed = await verifiedMutation(() => api('/api/ai/settings', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload),
    timeoutMs: 45000, idempotent: true,
  }), async () => {
    const saved = await api(`/api/ai/settings?station_id=${state.stationId}`);
    return { verified: aiSettingsMatch(saved, payload), value: saved };
  }, { attempts: 18, interval: 350, description: 'saved AI configuration' });
  state.ai = changed.value;
  clearFormDirty(['aiConfigEnabled', 'aiLlmModel', 'aiTtsProvider', 'aiVoicePersona', 'aiTtsModelPath', 'aiMaxSeconds', 'aiStationInterval', 'aiIncludeHistory', 'aiEducational', 'aiPromptTemplate']);
  renderAiConfiguration();
  return changed;
}

async function saveAiConfiguration(event) {
  event.preventDefault();
  setResult('aiConfigResult');
  let payload;
  try { payload = aiPayloadFromForm(); } catch (error) {
    setResult('aiConfigResult', errorMessage(error), 'error');
    return;
  }
  setBusy(true, 'Saving AI configuration…', 'Writing every setting and reading it back');
  try {
    await persistAiPayload(payload);
    await loadOperatorConfiguration();
    const message = `Verified: AI configuration was saved${payload.ai_host_enabled ? ' and enabled' : ' with AI disabled'}.`;
    setResult('aiConfigResult', message, 'success'); setResult('aiResult', message, 'success'); logActivity(message); toast('AI configuration verified');
  } catch (error) {
    const message = errorMessage(error); setResult('aiConfigResult', message, 'error'); logActivity(`AI configuration failed: ${message}`, 'error'); toast(message, 'error');
  } finally {
    setBusy(false);
    await loadCoreStatus().catch(() => {});
  }
}

async function runAiTest() {
  setResult('aiConfigResult');
  let payload;
  try { payload = aiPayloadFromForm(); } catch (error) {
    setResult('aiConfigResult', errorMessage(error), 'error'); return;
  }
  if (!payload.ai_host_enabled) {
    setResult('aiConfigResult', 'Enable AI host before generating a voice test.', 'error');
    return;
  }
  setBusy(true, 'Generating AI test voice…', 'Saving settings, warming the selected runtime, synthesizing audio, and verifying the result');
  try {
    await persistAiPayload(payload);
    const tested = await verifiedMutation(() => api('/api/setup/test-ai', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ station_id: state.stationId }), timeoutMs: 240000,
    }), async () => {
      const setup = await api(`/api/setup/state?station_id=${state.stationId}`);
      const check = (setup.checks || []).find((item) => item.name === 'ai_tts');
      return { verified: Boolean(check?.details?.test_passed), value: setup };
    }, { attempts: 20, interval: 1000, description: 'generated AI voice test' });
    state.setupState = tested.value;
    const aiCheck = (state.setupState.checks || []).find((item) => item.name === 'ai_tts');
    if (aiCheck?.details?.runtime_ready) {
      state.setupState = await api('/api/setup/verify', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ station_id: state.stationId, check: 'ai_tts' }), idempotent: true, timeoutMs: 45000,
      });
    }
    renderReadiness();
    const message = aiCheck?.message || 'Verified: the AI voice test generated playable audio.';
    setResult('aiConfigResult', message, 'success'); logActivity(message); toast('AI voice test verified');
  } catch (error) {
    await loadOperatorConfiguration().catch(() => {});
    const aiCheck = (state.setupState?.checks || []).find((item) => item.name === 'ai_tts');
    const message = aiCheck?.message || errorMessage(error);
    setResult('aiConfigResult', message, 'error'); logActivity(`AI voice test failed: ${message}`, 'error'); toast(message, 'error');
  } finally { setBusy(false); }
}

async function refreshWatchdogStatus() {
  setResult('watchdogResult', 'Refreshing watchdog evidence…');
  state.watchdog = await api('/api/watchdog/status');
  renderWatchdog();
  setResult('watchdogResult', 'Watchdog evidence refreshed.', 'success');
}

async function repairWatchdogProblems() {
  const watchdog = state.watchdog || await api('/api/watchdog/status');
  const failedStationIds = (watchdog.stations || [])
    .filter((item) => {
      const runtime = item.runtime || {};
      return !(runtime.running && runtime.worker_running && runtime.program_running && runtime.input_present && runtime.output_running)
        || runtime.mount_healthy === false;
    })
    .map((item) => Number(item.station_id))
    .filter((stationId) => Number.isInteger(stationId) && stationId > 0);
  const repairProfiles = !Boolean(watchdog.managed_profiles_ok);
  if (!failedStationIds.length && !repairProfiles) {
    setResult('watchdogResult', 'No confirmed problem requires repair.', 'success');
    return;
  }
  setResult('watchdogResult', 'Repairing confirmed problems and preserving healthy stations…');
  await api('/api/watchdog/repair', {
    method: 'POST',
    body: JSON.stringify({ station_ids: failedStationIds, repair_managed_profiles: repairProfiles }),
  });
  state.watchdog = await api('/api/watchdog/status');
  renderWatchdog();
  setResult('watchdogResult', 'Repair completed. Review the refreshed status.', 'success');
}

async function refreshReadiness() {
  setBusy(true, 'Running installation self-check…', 'Inspecting media tools, outputs, AI, and startup readiness');
  setResult('readinessResult');
  try {
    await loadOperatorConfiguration();
    const blocking = state.setupState?.blocking_reasons || state.setupState?.blocking || [];
    const message = state.setupState?.can_complete ? 'Verified: every required station check is ready.' : `Self-check finished: ${blocking.length} required item(s) need attention.`;
    setResult('readinessResult', message, state.setupState?.can_complete ? 'success' : 'error');
    logActivity(message, state.setupState?.can_complete ? 'success' : 'error');
  } catch (error) {
    const message = errorMessage(error); setResult('readinessResult', message, 'error'); logActivity(`Self-check failed: ${message}`, 'error'); toast(message, 'error');
  } finally { setBusy(false); }
}

async function repairDependencies() {
  setBusy(true, 'Repairing managed dependencies…', 'Checking and installing the runtimes supplied by this radio package');
  setResult('readinessResult');
  try {
    const repaired = await api('/api/setup/repair-dependencies', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ station_id: state.stationId }), timeoutMs: 600000, idempotent: true, transportAttempts: 2,
    });
    state.setupState = repaired || {};
    renderReadiness();
    const blocking = state.setupState.blocking || [];
    const message = state.setupState.can_complete ? 'Verified: managed dependencies and all required checks are ready.' : `Repair finished; ${blocking.length} station item(s) still need attention.`;
    setResult('readinessResult', message, state.setupState.can_complete ? 'success' : 'error');
    logActivity(message, state.setupState.can_complete ? 'success' : 'error'); toast('Dependency repair finished');
  } catch (error) {
    const message = errorMessage(error); setResult('readinessResult', message, 'error'); logActivity(`Dependency repair failed: ${message}`, 'error'); toast(message, 'error');
  } finally { setBusy(false); }
}

async function reloadBackendSafely() {
  const confirmed = window.confirm(
    'Reload the OnAir backend now? Active items remain in queue and restart from their beginning. Only stations with restart authorization enabled resume automatically.',
  );
  if (!confirmed) return;
  setBusy(true, 'Preparing a safe backend reload...', 'Stopping station processes, preserving queue order, and handing restart ownership to the supervisor');
  setResult('readinessResult');
  try {
    const accepted = await api('/api/maintenance/backend/reload', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({}),
      timeoutMs: 30000,
      transportAttempts: 1,
    });
    const previousInstance = String(accepted?.previous_backend_instance_id || '');
    setResult('readinessResult', 'Queue state is preserved. Waiting for the supervised backend replacement...');
    await sleep(Math.max(3000, Number(accepted?.restart_delay_seconds || 3) * 1000));
    const health = await poll(async () => {
      try {
        const snapshot = await api(`/api/health?station_id=${state.stationId}`, {
          timeoutMs: 2000,
          transportAttempts: 1,
        });
        const replacement = String(snapshot?.backend_instance_id || '');
        return {
          verified: Boolean(replacement && previousInstance && replacement !== previousInstance),
          value: snapshot,
        };
      } catch (_) {
        return { verified: false };
      }
    }, { attempts: 60, interval: 1000, description: 'supervised backend replacement' });
    state.health = health;
    await refreshAll(false);
    const message = 'Verified: the supervisor replaced the backend process and preserved queue ownership. Broadcast status was refreshed from the new process.';
    setResult('readinessResult', message, 'success');
    logActivity(message, 'success');
    toast('Backend reload verified');
  } catch (error) {
    const message = errorMessage(error);
    setResult('readinessResult', message, 'error');
    logActivity(`Safe backend reload failed: ${message}`, 'error');
    toast(message, 'error');
  } finally {
    setBusy(false);
  }
}

async function changePassword(event) {
  event.preventDefault();
  const currentPassword = $('currentPassword').value;
  const newPassword = $('newPassword').value;
  if (newPassword !== $('repeatPassword').value) {
    setResult('passwordResult', 'The two new password fields do not match.', 'error'); return;
  }
  if (newPassword.length < 8) {
    setResult('passwordResult', 'The new password must contain at least 8 characters.', 'error'); return;
  }
  setBusy(true, 'Changing password…', 'Updating credentials and revoking older sessions');
  setResult('passwordResult');
  try {
    await api('/api/auth/password', {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ current_password: currentPassword, new_password: newPassword }), timeoutMs: 20000,
    });
    $('passwordForm').reset();
    clearSession();
    setBusy(false);
    showLogin();
    $('loginError').textContent = 'Password changed. Sign in with your new password.';
  } catch (error) {
    const message = errorMessage(error); setResult('passwordResult', message, 'error'); logActivity(`Password change failed: ${message}`, 'error'); toast(message, 'error'); setBusy(false);
  }
}

async function syncJingleFolder(event) {
  event.preventDefault();
  const folder = $('jingleFolder').value.trim();
  if (!folder) { setResult('jingleResult', 'Enter the full jingle folder path.', 'error'); return; }
  const replace = $('jingleFolderReplace').checked;
  setBusy(true, 'Synchronizing jingle folder…', 'Importing, reconciling, and verifying station jingles');
  setResult('jingleResult');
  try {
    const result = await api('/api/library/folder/sync', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, timeoutMs: 600000, idempotent: true, transportAttempts: 2,
      body: JSON.stringify({ station_id: state.stationId, folder, recursive: true, track_type: 'jingle', mode: replace ? 'replace' : 'merge', remove_pending_queue: replace, profile_label: 'Jingles' }),
    });
    if (!result?.verified || result.track_type !== 'jingle') throw new Error('Backend did not verify the jingle folder');
    delete $('jingleFolder').dataset.dirty;
    await Promise.all([loadCoreStatus(), loadJingles(), loadQueue()]);
    const message = `Verified: ${result.active_files} station jingle(s) are active; ${result.added} added and ${result.deactivated} deactivated.`;
    setResult('jingleResult', message, 'success'); logActivity(message); toast('Jingle folder verified');
  } catch (error) {
    const message = errorMessage(error); setResult('jingleResult', message, 'error'); logActivity(`Jingle folder sync failed: ${message}`, 'error'); toast(message, 'error');
  } finally { setBusy(false); }
}

async function createStation(event) {
  event.preventDefault();
  const name = $('stationName').value.trim();
  if (!name) return;
  const configure = $('configureIcecast').checked;
  setBusy(true, 'Creating station…', configure ? 'Creating, configuring output, and verifying' : 'Creating and verifying');
  setResult('stationResult');
  let createdId = null;
  try {
    const created = await api('/api/stations', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name, description: $('stationDescription').value.trim() }) });
    createdId = Number(created?.station?.id || created?.id || 0);
    if (!createdId) throw new Error('Backend did not return the new station ID');
    if (configure) {
      const profile = $('icecastProfile').value;
      const sourceProtocol = $('icecastProtocol').value;
      if (sourceProtocol === 'shoutcast' && profile.startsWith('opus_')) throw new Error('SHOUTcast legacy source requires MP3 or AAC');
      await api('/api/stations/output', { method: 'POST', headers: { 'Content-Type': 'application/json' }, idempotent: true, body: JSON.stringify({
        station_id: createdId, local_output_enabled: false, output_device_id: '', icecast_enabled: true,
        icecast_host: $('icecastHost').value.trim(), icecast_port: Number($('icecastPort').value), icecast_mount: $('icecastMount').value.trim(),
        icecast_user: $('icecastUser').value.trim(), icecast_password: $('icecastPassword').value, output_gain_db: 0,
        stream_codec_profile: profile, stream_bitrate_kbps: streamProfileBitrate(profile), source_protocol: sourceProtocol,
      }) });
      await api('/api/settings/station', { method: 'POST', headers: { 'Content-Type': 'application/json' }, idempotent: true, body: JSON.stringify({ station_id: createdId, output_mode: sourceProtocol, source_protocol: sourceProtocol }) });
    }
    if ($('activateNewStation').checked) await api('/api/stations/active', { method: 'POST', headers: { 'Content-Type': 'application/json' }, idempotent: true, body: JSON.stringify({ station_id: createdId }) });
    await poll(async () => {
      const stations = await api('/api/stations');
      const found = (stations.stations || []).find((station) => Number(station.id) === createdId && station.name === name);
      if (!found) return { verified: false };
      if (!configure) return { verified: true, value: found };
      const output = await api(`/api/stations/output?station_id=${createdId}`);
      const verified = output.icecast_enabled && output.icecast_host === $('icecastHost').value.trim() && Number(output.icecast_port) === Number($('icecastPort').value) && output.icecast_mount === $('icecastMount').value.trim() && output.source_protocol === $('icecastProtocol').value;
      return { verified, value: found };
    }, { attempts: 12, interval: 350, description: 'new station and output configuration' });
    await loadStations(createdId);
    $('stationForm').reset();
    $('configureIcecast').checked = true;
    $('icecastHost').value = '127.0.0.1'; $('icecastPort').value = '8000'; $('icecastMount').value = '/new-station'; delete $('icecastMount').dataset.edited; $('icecastUser').value = 'source'; $('icecastProfile').value = 'aac_low_192'; $('icecastProtocol').value = 'icecast';
    toggleIcecastFields();
    setResult('stationResult', `Verified: ${name} was created${configure ? ' with its network output' : ''}.`, 'success');
    logActivity(`Created station ${name}${configure ? ' and verified network output' : ''}`);
    toast('Station creation verified');
    await refreshAll(true);
  } catch (error) {
    let message = errorMessage(error);
    if (createdId) {
      try {
        await api(`/api/stations/${createdId}`, { method: 'DELETE' });
        message += ' The incomplete station was rolled back.';
      } catch (rollbackError) {
        message += ` Rollback also failed: ${errorMessage(rollbackError)}`;
      }
    }
    setResult('stationResult', message, 'error');
    logActivity(`Station creation failed: ${message}`, 'error');
    toast(message, 'error');
  } finally {
    setBusy(false);
  }
}

function toggleIcecastFields() {
  const enabled = $('configureIcecast').checked;
  $('icecastFields').hidden = !enabled;
  $('icecastFields').querySelectorAll('input,select').forEach((node) => { node.disabled = !enabled; });
  updateSourceProfileCompatibility('icecastProtocol', 'icecastProfile');
}

function updateSourceProfileCompatibility(protocolId, profileId, tlsId = '') {
  const protocol = $(protocolId)?.value || 'icecast';
  const profile = $(profileId);
  if (profile) {
    [...profile.options].forEach((option) => {
      option.disabled = protocol === 'shoutcast' && option.value.startsWith('opus_');
    });
    if (profile.selectedOptions[0]?.disabled) profile.value = 'aac_low_192';
  }
  const tls = tlsId ? $(tlsId) : null;
  if (tls) {
    if (protocol === 'shoutcast') tls.checked = false;
    tls.disabled = protocol === 'shoutcast';
  }
}

function toggleCurrentOutputFields() {
  const icecastEnabled = $('currentIcecastEnabled')?.checked;
  const localEnabled = $('currentLocalEnabled')?.checked;
  if ($('currentIcecastFields')) {
    $('currentIcecastFields').hidden = !icecastEnabled;
    $('currentIcecastFields').querySelectorAll('input,select').forEach((node) => {
      node.disabled = !icecastEnabled;
      if (node.id !== 'currentIcecastPassword' && node.type !== 'checkbox') {
        node.required = Boolean(icecastEnabled);
      }
    });
  }
  if ($('currentDeviceLabel')) $('currentDeviceLabel').hidden = !localEnabled;
  if ($('currentOutputDevice')) {
    $('currentOutputDevice').disabled = !localEnabled;
    $('currentOutputDevice').required = Boolean(localEnabled);
  }
  updateSourceProfileCompatibility('currentSourceProtocol', 'currentIcecastProfile', 'currentIcecastTlsEnabled');
}

function currentOutputPayload() {
  const icecastEnabled = $('currentIcecastEnabled').checked;
  const localEnabled = $('currentLocalEnabled').checked;
  if (!icecastEnabled && !localEnabled) throw new Error('Enable at least one output: internet stream or local monitor');
  const sourceProtocol = $('currentSourceProtocol').value;
  const mount = $('currentIcecastMount').value.trim();
  const password = $('currentIcecastPassword').value;
  if (icecastEnabled && sourceProtocol === 'icecast' && !mount.startsWith('/')) throw new Error('Icecast mount must start with /');
  const existingCredential = Boolean(state.stationOutput?.icecast_password_configured);
  if (icecastEnabled && !password && !existingCredential) {
    throw new Error('Enter the stream source password');
  }
  const profile = $('currentIcecastProfile').value;
  if (icecastEnabled && sourceProtocol === 'shoutcast' && profile.startsWith('opus_')) throw new Error('SHOUTcast legacy source requires MP3 or AAC');
  return {
    station_id: state.stationId,
    local_output_enabled: localEnabled,
    output_device_id: localEnabled ? $('currentOutputDevice').value : '',
    icecast_enabled: icecastEnabled,
    icecast_host: $('currentIcecastHost').value.trim(),
    icecast_port: Number($('currentIcecastPort').value),
    icecast_mount: mount,
    icecast_user: $('currentIcecastUser').value.trim(),
    icecast_password: password,
    icecast_tls_enabled: $('currentIcecastTlsEnabled').checked,
    output_gain_db: Number($('currentOutputGain').value || 0),
    stream_codec_profile: profile,
    stream_bitrate_kbps: streamProfileBitrate(profile),
    source_protocol: sourceProtocol,
  };
}

function outputMatches(saved, payload) {
  return Boolean(saved)
    && Boolean(saved.local_output_enabled) === payload.local_output_enabled
    && String(saved.output_device_id || '') === payload.output_device_id
    && Boolean(saved.icecast_enabled) === payload.icecast_enabled
    && String(saved.icecast_host || '') === payload.icecast_host
    && Number(saved.icecast_port) === payload.icecast_port
    && String(saved.icecast_mount || '') === payload.icecast_mount
    && String(saved.icecast_user || '') === payload.icecast_user
    && Boolean(saved.icecast_tls_enabled) === payload.icecast_tls_enabled
    && (!payload.icecast_enabled || Boolean(saved.icecast_password_configured))
    && String(saved.stream_codec_profile || '') === payload.stream_codec_profile
    && Number(saved.stream_bitrate_kbps) === payload.stream_bitrate_kbps
    && String(saved.source_protocol || 'icecast') === payload.source_protocol;
}

function clearFormDirty(ids) {
  ids.forEach((id) => { const node = $(id); if (node) delete node.dataset.dirty; });
}

async function saveCurrentOutputLegacy(event) {
  event.preventDefault();
  setResult('outputConfigResult');
  let payload;
  try { payload = currentOutputPayload(); } catch (error) {
    setResult('outputConfigResult', errorMessage(error), 'error');
    return;
  }
  const stationName = $('currentStationName').value.trim();
  if (!stationName) return;
  const runtimeBefore = await api(`/api/runtime/${state.stationId}/status`).catch(() => ({}));
  const wasRunning = Boolean(runtimeBefore.running && runtimeBefore.output_feed_active);
  setBusy(true, 'Saving station output…', wasRunning ? 'Saving, applying to the live runtime, and verifying every required output' : 'Saving and reading the configuration back');
  try {
    const stored = await verifiedMutation(async () => {
      await api(`/api/stations/${state.stationId}`, {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: stationName }), idempotent: true,
      });
      await api('/api/stations/output', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload), idempotent: true,
      });
    }, async () => {
      const [stationsPayload, output] = await Promise.all([
        api('/api/stations'), api(`/api/stations/output?station_id=${state.stationId}`),
      ]);
      const named = (stationsPayload.stations || []).some((station) => Number(station.id) === Number(state.stationId) && station.name === stationName);
      return { verified: named && outputMatches(output, payload), value: { stationsPayload, output } };
    }, { attempts: 18, interval: 350, description: 'saved station identity and output configuration' });
    state.stationOutput = stored.value.output;
    state.stations = stored.value.stationsPayload.stations || state.stations;

    let liveApplied = false;
    if (wasRunning && runtimeBefore.active_input_uri) {
      const current = state.queue.find((item) => item.is_current || String(item.status) === 'playing') || {};
      const applied = await verifiedMutation(() => api(`/api/runtime/${state.stationId}/operator-start-track`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ input_uri: runtimeBefore.active_input_uri, stream_title: current.title || '', stream_artist: current.artist || '' }),
        timeoutMs: 45000,
      }), async () => {
        const health = await api(`/api/health?station_id=${state.stationId}`);
        const branches = health.runtime_branch_health || health.runtime?.branch_health || {};
        const verified = Boolean(health.runtime?.running && health.runtime?.output_feed_active
          && (!payload.icecast_enabled || branches.icecast)
          && (!payload.local_output_enabled || branches.local));
        return { verified, value: health };
      }, { attempts: 50, interval: 500, description: 'applied live output branches' });
      state.health = applied.value;
      liveApplied = true;
    }

    clearFormDirty(['currentStationName', 'currentOutputGain', 'currentIcecastEnabled', 'currentIcecastHost', 'currentIcecastPort', 'currentIcecastMount', 'currentIcecastUser', 'currentIcecastPassword', 'currentIcecastProfile', 'currentSourceProtocol', 'currentIcecastTlsEnabled', 'currentLocalEnabled', 'currentOutputDevice']);
    await loadStations(state.stationId);
    await loadOperatorConfiguration();
    renderOutputConfiguration();
    const message = liveApplied
      ? 'Verified: station identity and output were saved and every required live branch is healthy.'
      : 'Verified: station identity and output were saved. They will be used on the next broadcast start.';
    setResult('outputConfigResult', message, 'success'); logActivity(message); toast('Station output verified');
  } catch (error) {
    const message = errorMessage(error); setResult('outputConfigResult', message, 'error'); logActivity(`Output configuration failed: ${message}`, 'error'); toast(message, 'error');
  } finally {
    setBusy(false);
    await loadCoreStatus().catch(() => {});
  }
}

async function testCurrentOutputLegacy() {
  setBusy(true, 'Testing stream destination…', 'Checking configuration, network reachability, and saved verification');
  setResult('outputConfigResult');
  try {
    const stateResult = await api(`/api/setup/state?station_id=${state.stationId}`);
    const streamCheck = (stateResult.checks || []).find((check) => check.name === 'stream_output');
    if (!$('currentIcecastEnabled').checked) throw new Error('Icecast is disabled for this station');
    if (!streamCheck?.details?.configured) throw new Error('Save a complete Icecast configuration before testing');
    if (!streamCheck?.details?.reachable) throw new Error(streamCheck?.message || 'The Icecast destination is not reachable');
    state.setupState = await api('/api/setup/verify', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ station_id: state.stationId, check: 'stream_output' }), idempotent: true,
    });
    renderReadiness();
    setResult('outputConfigResult', 'Verified: the stream destination is configured and reachable.', 'success');
    logActivity('Verified the current station stream destination'); toast('Stream destination verified');
  } catch (error) {
    const message = errorMessage(error); setResult('outputConfigResult', message, 'error'); logActivity(`Stream test failed: ${message}`, 'error'); toast(message, 'error');
  } finally { setBusy(false); }
}

function renderStreamWizardSummary() {
  const node = $('streamWizardSummary');
  if (!node || !$('currentIcecastHost')) return;
  const profileSelect = $('currentIcecastProfile');
  const profile = profileSelect.options[profileSelect.selectedIndex]?.textContent || profileSelect.value;
  const protocol = $('currentSourceProtocol').value === 'shoutcast' ? 'SHOUTcast legacy source' : 'Icecast';
  const secure = protocol === 'Icecast' && $('currentIcecastTlsEnabled').checked ? 'secure ' : '';
  const destination = `${secure}${protocol} at ${$('currentIcecastHost').value || 'destination'}:${$('currentIcecastPort').value || '—'}${$('currentIcecastMount').value || ''}`;
  node.innerHTML = `<b>Listener result:</b> ${escapeHtml(profile)} will be sent to ${escapeHtml(destination)}. The current stream remains unchanged until Apply safely completes.`;
}

function renderStreamValidation(report) {
  const node = $('streamWizardChecks');
  if (!node) return;
  const labels = {
    locally_valid: 'Settings complete', destination_reachable: 'Destination reachable', credentials_verified: 'Protected password',
    standby_ready: 'Standby and quorum', rollback_ready: 'Rollback ready', live_output_verified: 'Live listener output', required_media: 'Queued media available',
  };
  node.innerHTML = Object.entries(report?.checks || {}).map(([key, check]) =>
    `<div class="stream-check ${escapeHtml(check.status || '')}"><b>${escapeHtml(labels[key] || key)} — ${escapeHtml(check.status || 'pending')}</b><br><small>${escapeHtml(check.message || '')}</small></div>`).join('');
}

async function createAndValidateStreamDraft() {
  const payload = currentOutputPayload();
  const draft = await api('/api/stream-config/drafts', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload), idempotent: true,
  });
  const report = await api(`/api/stream-config/drafts/${draft.id}/validate`, { method: 'POST', idempotent: true, timeoutMs: 10000 });
  renderStreamValidation(report);
  return { draft, report };
}

async function saveCurrentOutput(event) {
  event.preventDefault();
  setResult('outputConfigResult');
  const stationName = $('currentStationName').value.trim();
  if (!stationName) return;
  setBusy(true, 'Applying stream safely…', 'Validating, preserving rollback state, applying once, and reading authoritative state back');
  try {
    const { draft, report } = await createAndValidateStreamDraft();
    if (report.outcome === 'unsafe') {
      const blockers = Object.values(report.checks || {})
        .filter((check) => check?.status === 'unsafe')
        .map((check) => String(check.message || '').trim())
        .filter(Boolean);
      throw new Error(blockers.join(' ') || 'Unsafe to apply. Correct the highlighted checks.');
    }
    await api(`/api/stations/${state.stationId}`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name: stationName }), idempotent: true });
    const operation = await api(`/api/stream-config/drafts/${draft.id}/apply`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Idempotency-Key': globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random()}` },
      body: JSON.stringify({ defer_listener_verification: true }), idempotent: true, timeoutMs: 30000,
    });
    if (!['applied', 'verifying'].includes(operation.status)) throw new Error(operation.result?.message || 'The new output was not applied; the previous configuration remains active.');
    clearFormDirty(['currentStationName', 'currentOutputGain', 'currentIcecastEnabled', 'currentIcecastHost', 'currentIcecastPort', 'currentIcecastMount', 'currentIcecastUser', 'currentIcecastPassword', 'currentIcecastProfile', 'currentSourceProtocol', 'currentIcecastTlsEnabled', 'currentLocalEnabled', 'currentOutputDevice']);
    await loadStations(state.stationId); await loadOperatorConfiguration(); renderOutputConfiguration();
    const message = operation.result?.listener_verification_deferred
      ? 'Saved immediately. The broadcast engine is using the new settings and will keep reconnecting in the background; listener verification is still pending.'
      : operation.status === 'verifying'
      ? 'Applied safely. OnAir is observing listener audio for 60 seconds and will roll back automatically if it degrades.'
      : operation.result?.listener_audio_verified
        ? 'Verified end to end: the stream configuration was applied, read back, and the listener received audio bytes.'
        : 'Saved and read back. Listener audio will be verified automatically when this station starts.';
    setResult('outputConfigResult', message, 'success'); logActivity(message); toast('Station output verified');
  } catch (error) {
    const message = errorMessage(error); setResult('outputConfigResult', message, 'error'); logActivity(`Safe stream application stopped: ${message}`, 'error'); toast(message, 'error');
  } finally { setBusy(false); await loadCoreStatus().catch(() => {}); }
}

async function testCurrentOutput() {
  setBusy(true, 'Checking stream safely…', 'No live setting will change during this check');
  setResult('outputConfigResult');
  try {
    const { report } = await createAndValidateStreamDraft();
    const messages = { ready: 'Ready: every required pre-apply check passed. You can use Apply safely.', needs_attention: 'Needs attention: correct the highlighted item. Your current stream was not changed.', unsafe: 'Unsafe to apply: redundancy or credential protection is not ready. Your current stream was not changed.' };
    setResult('outputConfigResult', messages[report.outcome] || 'Check complete.', report.outcome === 'ready' ? 'success' : 'error');
    logActivity(`Stream safety check: ${report.outcome}`); toast(`Stream check: ${report.outcome}`);
  } catch (error) {
    const message = errorMessage(error); setResult('outputConfigResult', message, 'error'); logActivity(`Stream check failed: ${message}`, 'error'); toast(message, 'error');
  } finally { setBusy(false); }
}

function disarmStationDelete() {
  state.stationDeleteArmedUntil = 0;
  if (state.stationDeleteArmTimer) window.clearTimeout(state.stationDeleteArmTimer);
  state.stationDeleteArmTimer = null;
  $('deleteStationButton').textContent = 'Delete this station';
}

async function deleteCurrentStation() {
  if (state.stations.length <= 1) {
    setResult('outputConfigResult', 'The final station cannot be deleted. Create and verify its replacement first.', 'error');
    return;
  }
  if (state.stationDeleteArmedUntil <= Date.now()) {
    state.stationDeleteArmedUntil = Date.now() + 20000;
    $('deleteStationButton').textContent = `Confirm delete ${selectedStationName()}`;
    setResult('outputConfigResult', 'Click the delete button again within 20 seconds. This removes this station and its station-scoped data.');
    state.stationDeleteArmTimer = window.setTimeout(disarmStationDelete, 20000);
    return;
  }
  const deletingId = Number(state.stationId);
  const deletingName = selectedStationName();
  disarmStationDelete();
  setBusy(true, `Deleting ${deletingName}…`, 'Stopping only this station and verifying it is gone');
  try {
    await verifiedMutation(() => api(`/api/stations/${deletingId}`, { method: 'DELETE', timeoutMs: 45000 }), async () => {
      const stations = await api('/api/stations');
      return { verified: !(stations.stations || []).some((station) => Number(station.id) === deletingId), value: stations };
    }, { attempts: 20, interval: 400, description: 'station deletion' });
    await loadStations();
    clearFormDirty(['currentStationName', 'currentOutputGain', 'currentIcecastEnabled', 'currentIcecastHost', 'currentIcecastPort', 'currentIcecastMount', 'currentIcecastUser', 'currentIcecastPassword', 'currentIcecastProfile', 'currentSourceProtocol', 'currentIcecastTlsEnabled', 'currentLocalEnabled', 'currentOutputDevice']);
    await refreshAll(true);
    const message = `Verified: ${deletingName} was deleted.`;
    setResult('outputConfigResult', message, 'success'); logActivity(message); toast(message);
  } catch (error) {
    const message = errorMessage(error); setResult('outputConfigResult', message, 'error'); logActivity(`Station deletion failed: ${message}`, 'error'); toast(message, 'error');
  } finally { setBusy(false); }
}

async function addTrackToQueue(trackId) {
  const track = [...state.library, ...state.jingles].find((item) => Number(item.id) === Number(trackId));
  const alreadyPending = state.queue.some((item) => Number(item.track_id) === Number(trackId) && !item.is_played && String(item.status) !== 'done');
  setBusy(true, 'Adding to queue…', track?.title || `Track ${trackId}`);
  setResult('libraryResult'); setResult('queueResult');
  try {
    const queued = await verifiedMutation(() => api('/api/queue/push', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ station_id: state.stationId, track_id: Number(trackId) }), idempotent: true,
    }), async () => {
      const queue = await api(`/api/queue?station_id=${state.stationId}`);
      state.queue = queue.items || [];
      state.queueRevision = String(queue?.revision || '');
      const found = state.queue.some((item) => Number(item.track_id) === Number(trackId) && !item.is_played && String(item.status) !== 'done');
      return { verified: found, value: found };
    }, { attempts: 18, interval: 300, description: 'track in broadcast queue' });
    renderQueue();
    renderTimeline();
    const deduped = alreadyPending || queued.mutationResult?.deduped;
    const summary = queueAcknowledgementText(queued);
    const message = `${deduped ? 'Track was already pending in the queue; no duplicate was created.' : `Verified: ${track?.title || 'Track'} was added to the queue.`} ${summary}`;
    setResult('queueResult', message, 'success');
    logActivity(message);
    toast(deduped ? 'Already queued' : 'Queue insertion verified');
  } catch (error) {
    const message = errorMessage(error);
    setResult('queueResult', message, 'error');
    logActivity(`Queue insertion failed: ${message}`, 'error');
    toast(message, 'error');
  } finally { setBusy(false); }
}

function queueAcknowledgementText(outcome) {
  const result = outcome?.mutationResult || outcome;
  if (outcome?.recoveredTransportError) return 'The saved queue was read back, but live delivery and worker acknowledgement are unknown.';
  const persistence = result?.persistence;
  const acknowledgement = result?.runtime_acknowledgement;
  if (!(persistence?.committed || acknowledgement?.persisted)) return 'The queue change could not be confirmed.';
  const delivery = acknowledgement.queue_event_published
    ? 'Live observers were notified.'
    : 'No live observer delivery was confirmed.';
  const worker = result?.worker_acknowledgement;
  if (worker?.observed) return `${delivery} The worker acknowledged this queue revision.`;
  return worker?.state === 'pending'
    ? `${delivery} The worker is running, but has not acknowledged this queue revision yet.`
    : `${delivery} The worker is not running, so this revision is pending.`;
}

async function queueAction(action, itemId) {
  setBusy(true, action === 'remove' ? 'Removing queue item…' : 'Reordering queue…', 'Saving and reading queue back');
  setResult('queueResult');
  const before = await api(`/api/queue?station_id=${state.stationId}`).catch(() => ({ items: [] }));
  const beforeItems = before.items || [];
  const target = beforeItems.find((item) => Number(item.id) === Number(itemId));
  try {
    const expectedRevision = String(before?.revision || '');
    const fromIndex = Number(target?.queue_index ?? target?.index);
    if (!target?.id || !expectedRevision || !Number.isInteger(fromIndex)) throw new Error('Queue changed or is no longer available; reload and try again');
    const toIndex = action === 'up' ? Math.max(0, fromIndex - 1) : fromIndex + 1;
    const changed = await verifiedMutation(async () => {
      if (action === 'remove') {
        await api(`/api/queue/${fromIndex}?station_id=${state.stationId}&item_id=${Number(target.id)}&expected_revision=${encodeURIComponent(expectedRevision)}`, { method: 'DELETE' });
      } else {
        await api('/api/queue/move', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ station_id: state.stationId, item_id: Number(target.id), to_index: toIndex, expected_revision: expectedRevision }) });
      }
    }, async () => {
      const queue = await api(`/api/queue?station_id=${state.stationId}`);
      state.queue = queue.items || [];
      state.queueRevision = String(queue?.revision || '');
      if (action === 'remove') {
        const exists = state.queue.some((item) => Number(item.id) === Number(target?.id) && !item.is_played);
        return { verified: !exists, value: queue };
      }
      const moved = state.queue.find((item) => Number(item.id) === Number(target.id));
      return { verified: Number(moved?.queue_index ?? moved?.index) === toIndex, value: queue };
    }, { attempts: 14, interval: 250, description: `queue ${action}` });
    renderQueue();
    renderTimeline();
    const summary = queueAcknowledgementText(changed);
    const message = `${action === 'remove' ? 'Verified: queue item removed.' : 'Verified: queue order changed.'} ${summary}`;
    setResult('queueResult', message, 'success'); logActivity(message); toast(message);
  } catch (error) {
    const message = errorMessage(error); setResult('queueResult', message, 'error'); logActivity(`Queue change failed: ${message}`, 'error'); toast(message, 'error');
  } finally { setBusy(false); }
}

function currentUser() {
  try { return JSON.parse(localStorage.getItem(AUTH_KEYS.user) || 'null') || {}; } catch (_) { return {}; }
}

function operatorHasPermission(permission) {
  const user = currentUser();
  return ['admin', 'superadmin'].includes(String(user.role || '').toLowerCase())
    || new Set(user.effective_permissions || []).has(permission);
}

function setAccessPanelVisibility(panelId, visible) {
  const panel = $(panelId);
  if (visible) delete panel.dataset.accessHidden;
  else panel.dataset.accessHidden = 'true';
  panel.hidden = !visible || state.activeView !== 'settings';
}

function selectedAdminUser() {
  const id = Number($('userAdminSelect').value || 0);
  return state.users.find((user) => Number(user.id) === id) || null;
}

function selectedAdminRole() {
  const id = Number($('roleAdminSelect').value || 0);
  return state.roleTemplates.find((role) => Number(role.id) === id) || null;
}

function renderUserRoleChoices(user = null) {
  const selected = new Set((user?.role_template_ids || []).map(Number));
  const canManage = operatorHasPermission('users.manage');
  const roles = state.roleTemplates.filter((role) => role.is_active || selected.has(Number(role.id)));
  $('userRoleTemplateChoices').innerHTML = roles.length
    ? roles.map((role) => `<label><input type="checkbox" data-user-role-template="${Number(role.id)}" ${selected.has(Number(role.id)) ? 'checked' : ''} ${canManage ? '' : 'disabled'}><span><b>${escapeHtml(role.name)}</b><small>${escapeHtml(role.description || (role.is_system ? 'System template' : 'Custom template'))}</small></span></label>`).join('')
    : '<span class="card-copy">No role templates are available to this operator.</span>';
}

function renderUserAdminEditor() {
  const user = selectedAdminUser();
  const canManage = operatorHasPermission('users.manage');
  const canReset = canManage || operatorHasPermission('users.reset_password');
  const isNew = !user;
  $('userAdminUsername').value = user?.username || '';
  $('userAdminUsername').readOnly = !isNew || !canManage;
  $('userAdminDisplayName').value = user?.display_name || '';
  $('userAdminDisplayName').disabled = !canManage;
  $('userAdminLegacyRole').value = user?.legacy_role || user?.role || 'viewer';
  $('userAdminLegacyRole').disabled = !canManage;
  $('userAdminActive').checked = user ? Boolean(user.is_active) : true;
  $('userAdminActive').disabled = !canManage;
  $('userAdminPassword').value = '';
  $('userAdminPassword').required = isNew && canManage;
  $('userAdminPasswordLabel').hidden = !isNew || !canManage;
  $('saveUserAdminButton').hidden = !canManage;
  $('saveUserAdminButton').textContent = isNew ? 'Create operator' : 'Save operator';
  const isSelf = Number(user?.id || 0) === Number(currentUser().id || 0);
  $('deactivateUserAdminButton').hidden = !canManage || isNew || isSelf || !user.is_active;
  $('deactivateUserAdminButton').textContent = 'Deactivate operator';
  $('resetUserPasswordForm').hidden = !canReset || isNew;
  renderUserRoleChoices(user);
}

function renderRolePermissionChoices(role = null) {
  const selected = new Set(role?.permission_keys || []);
  const canManage = operatorHasPermission('roles.manage') && !role?.is_system;
  const groups = Object.entries(state.permissionGroups || {});
  $('rolePermissionChoices').innerHTML = groups.length
    ? groups.map(([group, permissions]) => `<section class="permission-group"><h3>${escapeHtml(group.replaceAll('_', ' '))}</h3><div class="permission-choice-grid">${permissions.map((permission) => `<label><input type="checkbox" data-role-permission="${escapeHtml(permission)}" ${selected.has(permission) ? 'checked' : ''} ${canManage ? '' : 'disabled'}><span>${escapeHtml(permission)}</span></label>`).join('')}</div></section>`).join('')
    : '<p class="card-copy">The permission catalog is unavailable.</p>';
}

function renderRoleAdminEditor() {
  const role = selectedAdminRole();
  const canManage = operatorHasPermission('roles.manage') && !role?.is_system;
  $('roleAdminName').value = role?.name || '';
  $('roleAdminDescription').value = role?.description || '';
  $('roleAdminName').disabled = !canManage;
  $('roleAdminDescription').disabled = !canManage;
  $('saveRoleAdminButton').hidden = !canManage;
  $('saveRoleAdminButton').textContent = role ? 'Save role template' : 'Create role template';
  $('deactivateRoleAdminButton').hidden = !canManage || !role || !role.is_active;
  $('deactivateRoleAdminButton').textContent = 'Deactivate role template';
  renderRolePermissionChoices(role);
}

function renderAdminAccess({ preferredUserId = null, preferredRoleId = null } = {}) {
  const userSelect = $('userAdminSelect');
  const previousUserId = Number(preferredUserId ?? userSelect.value ?? 0);
  userSelect.innerHTML = '<option value="0">Create a new operator</option>' + state.users.map((user) => `<option value="${Number(user.id)}">${escapeHtml(user.display_name || user.username)}${user.is_active ? '' : ' (inactive)'}</option>`).join('');
  userSelect.value = state.users.some((user) => Number(user.id) === previousUserId) ? String(previousUserId) : '0';
  $('userAdminCount').textContent = String(state.users.length);
  $('userAdminList').innerHTML = state.users.length ? state.users.map((user) => {
    const roleNames = (user.role_template_ids || []).map((id) => state.roleTemplates.find((role) => Number(role.id) === Number(id))?.name).filter(Boolean);
    return `<div class="media-row"><div class="media-title"><small>${user.is_active ? 'ACTIVE' : 'INACTIVE'} · ${escapeHtml(user.username)}</small><b>${escapeHtml(user.display_name || user.username)}</b><span>${escapeHtml(roleNames.join(', ') || user.legacy_role || user.role || 'No role')}</span></div></div>`;
  }).join('') : '<div class="empty-state">No operator accounts are visible.</div>';

  const roleSelect = $('roleAdminSelect');
  const previousRoleId = Number(preferredRoleId ?? roleSelect.value ?? 0);
  roleSelect.innerHTML = '<option value="0">Create a new role template</option>' + state.roleTemplates.map((role) => `<option value="${Number(role.id)}">${escapeHtml(role.name)}${role.is_active ? '' : ' (inactive)'}${role.is_system ? ' · system' : ''}</option>`).join('');
  roleSelect.value = state.roleTemplates.some((role) => Number(role.id) === previousRoleId) ? String(previousRoleId) : '0';
  $('roleAdminCount').textContent = String(state.roleTemplates.length);
  renderUserAdminEditor();
  renderRoleAdminEditor();
}

async function loadAdminAccess(force = false, selection = {}) {
  const canViewUsers = operatorHasPermission('users.manage') || operatorHasPermission('users.reset_password');
  const canViewRoles = operatorHasPermission('roles.manage') || operatorHasPermission('users.manage');
  setAccessPanelVisibility('userAdminPanel', canViewUsers);
  setAccessPanelVisibility('roleAdminPanel', canViewRoles);
  if ((!canViewUsers && !canViewRoles) || (state.adminAccessLoaded && !force)) return;
  try {
    const [usersPayload, rolesPayload] = await Promise.all([
      canViewUsers ? api('/api/users') : Promise.resolve({ items: [] }),
      canViewRoles ? api('/api/roles') : Promise.resolve({ items: [], permission_groups: {} }),
    ]);
    state.users = Array.isArray(usersPayload?.items) ? usersPayload.items : [];
    state.roleTemplates = Array.isArray(rolesPayload?.items) ? rolesPayload.items : [];
    state.permissionGroups = rolesPayload?.permission_groups || {};
    state.adminAccessLoaded = true;
    renderAdminAccess(selection);
  } catch (error) {
    state.adminAccessLoaded = false;
    const message = errorMessage(error);
    if (canViewUsers) setResult('userAdminResult', message, 'error');
    if (canViewRoles) setResult('roleAdminResult', message, 'error');
  }
}

function selectedUserRoleTemplateIds() {
  return [...document.querySelectorAll('[data-user-role-template]:checked')].map((input) => Number(input.dataset.userRoleTemplate));
}

async function saveAdminUser(event) {
  event.preventDefault();
  const existing = selectedAdminUser();
  const displayName = $('userAdminDisplayName').value.trim();
  const role = $('userAdminLegacyRole').value;
  const roleTemplateIds = selectedUserRoleTemplateIds();
  if (!displayName) return setResult('userAdminResult', 'Display name is required.', 'error');
  setResult('userAdminResult');
  try {
    let saved;
    if (existing) {
      saved = await api(`/api/users/${Number(existing.id)}`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, idempotent: true, body: JSON.stringify({ display_name: displayName, role, is_active: $('userAdminActive').checked, role_template_ids: roleTemplateIds }) });
    } else {
      const password = $('userAdminPassword').value;
      if (password.length < 8) return setResult('userAdminResult', 'Initial password must contain at least 8 characters.', 'error');
      saved = await api('/api/users', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ username: $('userAdminUsername').value.trim(), display_name: displayName, password, role, role_template_ids: roleTemplateIds }) });
    }
    $('userAdminPassword').value = '';
    await loadAdminAccess(true, { preferredUserId: Number(saved.id) });
    const readBack = state.users.find((user) => Number(user.id) === Number(saved.id));
    if (!readBack || readBack.display_name !== displayName) throw new Error('Operator account read-back did not match the saved value');
    setResult('userAdminResult', `Verified: ${readBack.display_name} was saved.`, 'success');
  } catch (error) { setResult('userAdminResult', errorMessage(error), 'error'); }
}

async function deactivateAdminUser() {
  const user = selectedAdminUser();
  if (!user || Number(user.id) === Number(currentUser().id)) return;
  if (state.userDeactivateArmedUntil <= Date.now()) {
    state.userDeactivateArmedUntil = Date.now() + 20000;
    $('deactivateUserAdminButton').textContent = 'Confirm deactivation';
    return setResult('userAdminResult', `Click “Confirm deactivation” within 20 seconds to disable ${user.display_name || user.username}.`);
  }
  state.userDeactivateArmedUntil = 0;
  try {
    await api(`/api/users/${Number(user.id)}`, { method: 'DELETE' });
    await loadAdminAccess(true, { preferredUserId: Number(user.id) });
    if (selectedAdminUser()?.is_active) throw new Error('Operator account remained active after deactivation');
    setResult('userAdminResult', 'Verified: operator account is inactive.', 'success');
  } catch (error) { setResult('userAdminResult', errorMessage(error), 'error'); }
}

async function resetAdminUserPassword(event) {
  event.preventDefault();
  const user = selectedAdminUser();
  const newPassword = $('resetUserPassword').value;
  if (!user || newPassword.length < 8) return setResult('userAdminResult', 'Select an operator and enter at least 8 characters.', 'error');
  try {
    await api(`/api/users/${Number(user.id)}/reset-password`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ new_password: newPassword }) });
    $('resetUserPassword').value = '';
    setResult('userAdminResult', 'Password reset completed. The temporary value is not displayed or logged.', 'success');
  } catch (error) { setResult('userAdminResult', errorMessage(error), 'error'); }
}

function selectedRolePermissionKeys() {
  return [...document.querySelectorAll('[data-role-permission]:checked')].map((input) => input.dataset.rolePermission);
}

async function saveAdminRole(event) {
  event.preventDefault();
  const existing = selectedAdminRole();
  const payload = { name: $('roleAdminName').value.trim(), description: $('roleAdminDescription').value.trim(), permission_keys: selectedRolePermissionKeys() };
  if (!payload.name) return setResult('roleAdminResult', 'Role name is required.', 'error');
  try {
    const saved = existing
      ? await api(`/api/roles/${Number(existing.id)}`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, idempotent: true, body: JSON.stringify(payload) })
      : await api('/api/roles', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
    await loadAdminAccess(true, { preferredRoleId: Number(saved.id) });
    const readBack = state.roleTemplates.find((role) => Number(role.id) === Number(saved.id));
    if (!readBack || readBack.name !== payload.name) throw new Error('Role template read-back did not match the saved value');
    setResult('roleAdminResult', `Verified: ${readBack.name} was saved.`, 'success');
  } catch (error) { setResult('roleAdminResult', errorMessage(error), 'error'); }
}

async function deactivateAdminRole() {
  const role = selectedAdminRole();
  if (!role || role.is_system) return;
  if (state.roleDeactivateArmedUntil <= Date.now()) {
    state.roleDeactivateArmedUntil = Date.now() + 20000;
    $('deactivateRoleAdminButton').textContent = 'Confirm deactivation';
    return setResult('roleAdminResult', `Click “Confirm deactivation” within 20 seconds to disable ${role.name}.`);
  }
  state.roleDeactivateArmedUntil = 0;
  try {
    await api(`/api/roles/${Number(role.id)}`, { method: 'DELETE' });
    await loadAdminAccess(true, { preferredRoleId: Number(role.id) });
    if (selectedAdminRole()?.is_active) throw new Error('Role template remained active after deactivation');
    setResult('roleAdminResult', 'Verified: role template is inactive.', 'success');
  } catch (error) { setResult('roleAdminResult', errorMessage(error), 'error'); }
}

function emergencyRecovery() {
  if (state.emergency.originalSettings) return state.emergency.originalSettings;
  try {
    const saved = JSON.parse(sessionStorage.getItem('deterministic_wall_emergency_recovery') || 'null');
    return saved?.settings || null;
  } catch (_) {
    return null;
  }
}

function saveEmergencyRecovery(stationId, settings) {
  state.emergency.originalSettings = settings;
  sessionStorage.setItem('deterministic_wall_emergency_recovery', JSON.stringify({ stationId: Number(stationId), settings }));
}

function clearEmergencyRecovery() {
  state.emergency.originalSettings = null;
  sessionStorage.removeItem('deterministic_wall_emergency_recovery');
}

function normalizeEmergencyUrl(raw) {
  const candidate = String(raw || '').trim();
  if (!candidate) throw new Error('Enter the Chrome page URL first');
  const withScheme = /^[a-z][a-z0-9+.-]*:\/\//i.test(candidate) ? candidate : `https://${candidate}`;
  const parsed = new URL(withScheme);
  if (!['http:', 'https:'].includes(parsed.protocol)) throw new Error('Emergency Room accepts only http or https pages');
  return parsed.href;
}

async function skipCurrentQueueItem(itemId) {
  setBusy(true, 'Skipping current audio…', 'Stopping the scheduler before changing the queue');
  try {
    const snapshot = await api(`/api/queue?station_id=${state.stationId}`);
    const item = (snapshot.items || []).find((entry) => Number(entry.id) === Number(itemId));
    if (!item?.is_current && String(item?.status) !== 'playing') throw new Error('Current queue item changed; reload and try again');
    await api(`/api/runtime/${state.stationId}/operator-skip-current`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ item_id: Number(itemId), expected_revision: String(snapshot.revision || '') }),
    });
    await loadQueue();
    const message = 'Current audio was skipped safely. The scheduler resumed and will select the next item.';
    setResult('queueResult', message, 'success'); toast(message); logActivity(message);
  } catch (error) {
    const message = errorMessage(error); setResult('queueResult', message, 'error'); toast(message, 'error');
  } finally { setBusy(false); }
}

function clearEmergencyArm() {
  state.emergency.armedUntil = 0;
  if (state.emergency.armTimer) window.clearTimeout(state.emergency.armTimer);
  state.emergency.armTimer = null;
}

function armEmergencyTakeover() {
  clearEmergencyArm();
  state.emergency.armedUntil = Date.now() + 20000;
  state.emergency.armTimer = window.setTimeout(() => {
    clearEmergencyArm();
    renderEmergencyStatus();
    setResult('emergencyResult', 'Emergency takeover confirmation expired. Nothing changed.');
  }, 20000);
  renderEmergencyStatus();
  setResult(
    'emergencyResult',
    'Takeover armed for 20 seconds. Click “Confirm emergency takeover” to open the page and request its audio.',
  );
}

function useEmergencyPreset() {
  const selected = String($('emergencyPreset').value || '').trim();
  if (!selected) {
    $('emergencyUrl').focus();
    setResult('emergencyResult', 'Custom source selected. Enter an approved HTTP or HTTPS page.');
    return;
  }
  const url = normalizeEmergencyUrl(selected);
  $('emergencyUrl').value = url;
  state.emergency.sourceUrl = url;
  clearEmergencyArm();
  renderEmergencyStatus();
  setResult('emergencyResult', `${$('emergencyPreset').selectedOptions[0].textContent} selected. Preview it before arming takeover.`, 'success');
}

function previewEmergencySource() {
  try {
    const url = normalizeEmergencyUrl($('emergencyUrl').value);
    $('emergencyUrl').value = url;
    state.emergency.sourceUrl = url;
    if (state.emergency.openedWindow && !state.emergency.openedWindow.closed) {
      state.emergency.openedWindow.location.href = url;
      state.emergency.openedWindow.focus();
    } else {
      state.emergency.openedWindow = window.open(url, '_blank', 'noopener=false');
    }
    if (!state.emergency.openedWindow) throw new Error('The browser blocked the preview window. Allow pop-ups for RadioTEDU OnAir and try again');
    clearEmergencyArm();
    renderEmergencyStatus();
    setResult('emergencyResult', 'Preview opened without changing the broadcast. Start playback on that page, then arm takeover.', 'success');
  } catch (error) {
    setResult('emergencyResult', errorMessage(error), 'error');
  }
}

async function ensureOperatorStudioOwnership(stationId) {
  let snapshot = await api(`/api/studios?station_id=${stationId}`);
  state.studios = Array.isArray(snapshot?.studios) ? snapshot.studios : [];
  let user = currentUser();
  if (!Number(user.id || 0)) {
    user = await api('/api/auth/me');
    localStorage.setItem(AUTH_KEYS.user, JSON.stringify(user));
  }
  const userId = Number(user.id || 0);
  if (!userId) throw new Error('The signed-in operator identity could not be verified');
  let studio = (snapshot.studios || []).find((item) => item.is_on_air)
    || (snapshot.studios || []).find((item) => item.is_active && (!item.current_user_id || Number(item.current_user_id) === userId));
  if (!studio) {
    const created = await api('/api/studios', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ station_id: Number(stationId), name: 'Operator Studio', description: 'RadioTEDU OnAir live, emergency, and guest control' }),
    });
    studio = created.studio;
  }
  if (!studio?.id) throw new Error('No studio is available for emergency audio');
  snapshot = await api(`/api/studios/${Number(studio.id)}/join`, { method: 'POST' });
  const verified = (snapshot.studios || []).find((item) => Number(item.id) === Number(studio.id));
  if (!verified?.is_on_air || Number(verified.current_user_id || 0) !== userId) {
    throw new Error('Operator Studio could not take ownership of the on-air studio');
  }
  state.studios = Array.isArray(snapshot?.studios) ? snapshot.studios : [];
  state.selectedStudioId = Number(verified.id);
  state.joinedStudioId = Number(verified.id);
  return verified;
}

function resampleToPcm16(input, inputRate, outputRate = 24000) {
  const sourceRate = Math.max(1, Number(inputRate || 48000));
  const ratio = sourceRate / outputRate;
  const outputLength = Math.max(1, Math.floor(input.length / ratio));
  const pcm = new Int16Array(outputLength);
  for (let index = 0; index < outputLength; index += 1) {
    const sourceIndex = index * ratio;
    const leftIndex = Math.floor(sourceIndex);
    const rightIndex = Math.min(input.length - 1, leftIndex + 1);
    const fraction = sourceIndex - leftIndex;
    const sample = Math.max(-1, Math.min(1, input[leftIndex] + (input[rightIndex] - input[leftIndex]) * fraction));
    pcm[index] = sample < 0 ? Math.round(sample * 32768) : Math.round(sample * 32767);
  }
  return pcm.buffer;
}

function enqueueEmergencyChunk(chunk) {
  if (!state.emergency.active && !state.emergency.starting) return;
  if (state.emergency.pendingChunks.length >= 36) {
    state.emergency.pendingChunks.shift();
    state.emergency.droppedChunks += 1;
  }
  state.emergency.pendingChunks.push(chunk);
  drainEmergencyChunks();
}

async function drainEmergencyChunks() {
  if (state.emergency.draining) return;
  state.emergency.draining = true;
  try {
    while ((state.emergency.active || state.emergency.starting) && state.emergency.pendingChunks.length) {
      const chunk = state.emergency.pendingChunks.shift();
      await api(`/api/audio/live/render/chunk?station_id=${Number(state.emergency.stationId)}`, {
        method: 'POST', headers: { 'Content-Type': 'application/octet-stream' }, body: chunk, timeoutMs: 8000,
      });
    }
  } catch (error) {
    const message = `Emergency audio transport failed: ${errorMessage(error)}`;
    setResult('emergencyResult', message, 'error');
    logActivity(message, 'error');
    window.setTimeout(() => stopEmergency('transport failure').catch(() => {}), 0);
  } finally {
    state.emergency.draining = false;
  }
}

async function attachEmergencyAudio(stream) {
  const AudioContextClass = window.AudioContext || window.webkitAudioContext;
  if (!AudioContextClass) throw new Error('Chrome audio processing is unavailable');
  const context = new AudioContextClass();
  await context.resume();
  const source = context.createMediaStreamSource(stream);
  const processor = context.createScriptProcessor(8192, 1, 1);
  const silentGain = context.createGain();
  silentGain.gain.value = 0;
  processor.onaudioprocess = (event) => {
    if (!state.emergency.active && !state.emergency.starting) return;
    const samples = event.inputBuffer.getChannelData(0);
    enqueueEmergencyChunk(resampleToPcm16(samples, context.sampleRate));
  };
  source.connect(processor);
  processor.connect(silentGain);
  silentGain.connect(context.destination);
  state.emergency.audioContext = context;
  state.emergency.sourceNode = source;
  state.emergency.processorNode = processor;
  state.emergency.silentGainNode = silentGain;
}

function releaseEmergencyMedia() {
  const emergency = state.emergency;
  if (emergency.processorNode) emergency.processorNode.onaudioprocess = null;
  [emergency.sourceNode, emergency.processorNode, emergency.silentGainNode].forEach((node) => {
    try { node?.disconnect(); } catch (_) { /* Already disconnected. */ }
  });
  try { emergency.stream?.getTracks().forEach((track) => track.stop()); } catch (_) { /* Already stopped. */ }
  try { emergency.audioContext?.close(); } catch (_) { /* Already closed. */ }
  emergency.stream = null;
  emergency.audioContext = null;
  emergency.sourceNode = null;
  emergency.processorNode = null;
  emergency.silentGainNode = null;
  emergency.pendingChunks = [];
}

function renderEmergencyStatus(runtime = state.runtime || {}) {
  const serverLive = Boolean(runtime?.live_mic_active);
  const live = Boolean(state.emergency.active || serverLive);
  const waiting = Boolean(state.emergency.starting || state.emergency.stopping);
  $('emergencyLamp').className = `status-lamp ${live ? 'live' : waiting ? 'warming' : 'off'}`;
  $('emergencyLamp').innerHTML = `<span></span><b>${live ? 'LIVE' : waiting ? 'WORKING' : 'OFF'}</b>`;
  $('emergencyProgramState').textContent = String(runtime?.program_music_mode || '').toLowerCase() === 'mute' ? 'Muted' : 'Normal';
  $('emergencySignalState').textContent = runtime?.live_mic_receiving ? `${Number(runtime.live_mic_peak_db || -60).toFixed(0)} dB` : live ? 'Waiting for sound' : 'Off';
  const bufferMs = Math.max(0, Math.round(Number(runtime?.live_mic_buffer_bytes || 0) / 48));
  $('emergencyBufferState').textContent = `${bufferMs} ms${state.emergency.droppedChunks ? ` · ${state.emergency.droppedChunks} dropped` : ''}`;
  const armed = !live && !waiting && Date.now() < Number(state.emergency.armedUntil || 0);
  $('emergencySourceState').textContent = state.emergency.sourceUrl
    ? state.emergency.sourceUrl.replace(/^https?:\/\//i, '').replace(/\/$/, '')
    : 'Not selected';
  $('startEmergencyButton').textContent = armed ? 'Confirm emergency takeover' : 'Arm emergency takeover';
  $('startEmergencyButton').disabled = live || waiting;
  $('stopEmergencyButton').disabled = !live || state.emergency.starting || state.emergency.stopping;
  $('emergencyUrl').disabled = live || waiting;
  $('emergencyPreset').disabled = live || waiting;
  $('emergencyPresetButton').disabled = live || waiting;
  $('previewEmergencyButton').disabled = live || waiting;
  document.querySelector('.emergency-panel').classList.toggle('is-live', live);
  const emergencyNav = document.querySelector('[data-operator-nav="emergency"]');
  if (emergencyNav) emergencyNav.dataset.live = live ? 'true' : 'false';
}

async function refreshEmergencyStatus() {
  if (!state.emergency.active) return;
  try {
    const runtime = await api(`/api/runtime/${Number(state.emergency.stationId)}/status`);
    state.runtime = Number(state.emergency.stationId) === Number(state.stationId) ? runtime : state.runtime;
    renderEmergencyStatus(runtime);
  } catch (_) { /* Main refresh will surface backend connectivity. */ }
}

function startEmergencyStatusTimer() {
  if (state.emergency.statusTimer) window.clearInterval(state.emergency.statusTimer);
  state.emergency.statusTimer = window.setInterval(refreshEmergencyStatus, 1000);
}

function stopEmergencyStatusTimer() {
  if (state.emergency.statusTimer) window.clearInterval(state.emergency.statusTimer);
  state.emergency.statusTimer = null;
}

async function startEmergency() {
  if (state.emergency.active || state.emergency.starting) return;
  if (Date.now() >= Number(state.emergency.armedUntil || 0)) {
    armEmergencyTakeover();
    return;
  }
  clearEmergencyArm();
  state.emergency.starting = true;
  state.emergency.stationId = Number(state.stationId);
  state.emergency.droppedChunks = 0;
  setResult('emergencyResult', 'Opening the page and waiting for browser tab-audio permission...');
  renderEmergencyStatus();
  let settingsChanged = false;
  let renderStarted = false;
  try {
    const url = normalizeEmergencyUrl($('emergencyUrl').value);
    $('emergencyUrl').value = url;
    state.emergency.sourceUrl = url;
    if (state.emergency.openedWindow && !state.emergency.openedWindow.closed) {
      state.emergency.openedWindow.focus();
    } else {
      state.emergency.openedWindow = window.open(url, '_blank', 'noopener=false');
    }
    if (!state.emergency.openedWindow) throw new Error('The browser blocked the source window. Allow pop-ups for RadioTEDU OnAir and try again');
    if (!navigator.mediaDevices?.getDisplayMedia) throw new Error('Browser tab-audio sharing is not supported in this browser');
    const stream = await navigator.mediaDevices.getDisplayMedia({
      video: { displaySurface: 'browser' },
      audio: { channelCount: 1, echoCancellation: false, noiseSuppression: false, autoGainControl: false },
      preferCurrentTab: false,
      selfBrowserSurface: 'exclude',
      surfaceSwitching: 'include',
      systemAudio: 'exclude',
    });
    if (!stream.getAudioTracks().length) {
      stream.getTracks().forEach((track) => track.stop());
      throw new Error('No tab audio was shared. Select the opened browser tab and enable Share tab audio');
    }
    state.emergency.stream = stream;
    const original = await api(`/api/audio/live/status?station_id=${state.emergency.stationId}`);
    saveEmergencyRecovery(state.emergency.stationId, {
      station_id: state.emergency.stationId,
      program_music_mode: original.program_music_mode || 'normal',
      mic_gain: Number(original.mic_gain ?? 1),
      music_gain: Number(original.music_gain ?? 1),
      duck_level: Number(original.duck_level ?? 0.15),
    });
    await ensureOperatorStudioOwnership(state.emergency.stationId);
    await api('/api/audio/live/settings', {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ...state.emergency.originalSettings, program_music_mode: 'mute', mic_gain: 1 }),
    });
    settingsChanged = true;
    await api('/api/audio/live/render/start', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ station_id: state.emergency.stationId, source_name: 'Emergency Room browser audio', input_format: 's16le', sample_rate: 24000, channels: 1, max_buffer_bytes: 480000 }),
    });
    renderStarted = true;
    await attachEmergencyAudio(stream);
    state.emergency.active = true;
    stream.getTracks().forEach((track) => { track.onended = () => stopEmergency('browser stopped sharing').catch(() => {}); });
    const verifiedRuntime = await poll(async () => {
      const runtime = await api(`/api/runtime/${state.emergency.stationId}/status`);
      return {
        verified: Boolean(
          runtime.live_mic_active
          && runtime.program_music_mode === 'mute'
          && runtime.output_feed_active
          && (runtime.live_mic_receiving || Number(runtime.live_mic_buffer_bytes || 0) > 0)
        ),
        value: runtime,
      };
    }, { attempts: 30, interval: 250, description: 'exclusive emergency browser audio' });
    state.emergency.starting = false;
    state.runtime = verifiedRuntime;
    renderEmergencyStatus(verifiedRuntime);
    startEmergencyStatusTimer();
    setResult('emergencyResult', 'Verified: audio frames are arriving; normal playout is muted and only the shared browser page is on air.', 'success');
    logActivity(`Emergency Room started for ${selectedStationName()}`);
    toast('Emergency browser audio is live');
  } catch (error) {
    state.emergency.active = false;
    state.emergency.starting = false;
    releaseEmergencyMedia();
    if (renderStarted) await api('/api/audio/live/render/stop', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ station_id: state.emergency.stationId }) }).catch(() => {});
    if (settingsChanged && emergencyRecovery()) await api('/api/audio/live/settings', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(emergencyRecovery()) }).catch(() => {});
    clearEmergencyRecovery();
    const message = errorMessage(error);
    setResult('emergencyResult', message, 'error');
    logActivity(`Emergency Room failed: ${message}`, 'error');
    toast(message, 'error');
    await loadCoreStatus().catch(() => {});
    renderEmergencyStatus();
  }
}

async function stopEmergency(reason = 'operator stop') {
  if (state.emergency.stopping) return;
  state.emergency.stopping = true;
  state.emergency.starting = false;
  const stationId = Number(state.emergency.stationId || state.stationId);
  const restore = emergencyRecovery() || {
    station_id: stationId, program_music_mode: 'normal', mic_gain: 1, music_gain: 1, duck_level: 0.15,
  };
  stopEmergencyStatusTimer();
  releaseEmergencyMedia();
  renderEmergencyStatus();
  setResult('emergencyResult', 'Stopping tab audio and restoring the normal program...');
  try {
    await api('/api/audio/live/render/stop', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ station_id: stationId }),
    });
    await api('/api/audio/live/settings', {
      method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ ...restore, station_id: stationId }),
    });
    const verifiedRuntime = await poll(async () => {
      const runtime = await api(`/api/runtime/${stationId}/status`);
      return { verified: !runtime.live_mic_active && runtime.program_music_mode === restore.program_music_mode && runtime.output_feed_active, value: runtime };
    }, { attempts: 30, interval: 250, description: 'normal program restoration' });
    state.emergency.active = false;
    state.runtime = Number(stationId) === Number(state.stationId) ? verifiedRuntime : state.runtime;
    clearEmergencyRecovery();
    setResult('emergencyResult', `Verified: emergency audio stopped and the ${restore.program_music_mode} program mix was restored.`, 'success');
    logActivity(`Emergency Room stopped (${reason})`);
    toast('Normal program restored');
  } catch (error) {
    const message = errorMessage(error);
    setResult('emergencyResult', `Emergency stop needs attention: ${message}`, 'error');
    logActivity(`Emergency stop failed: ${message}`, 'error');
    toast(message, 'error');
  } finally {
    state.emergency.active = false;
    state.emergency.stopping = false;
    state.emergency.stationId = null;
    await loadCoreStatus().catch(() => {});
    renderEmergencyStatus();
  }
}

function emergencyPageHideCleanup() {
  if (!state.emergency.active && !state.emergency.starting) return;
  releaseEmergencyMedia();
  const stationId = Number(state.emergency.stationId || state.stationId);
  const token = localStorage.getItem(AUTH_KEYS.access);
  const restore = emergencyRecovery();
  const headers = { 'Content-Type': 'application/json' };
  if (token) headers.Authorization = `Bearer ${token}`;
  fetch('/api/audio/live/render/stop', { method: 'POST', headers, body: JSON.stringify({ station_id: stationId }), keepalive: true }).catch(() => {});
  if (restore) fetch('/api/audio/live/settings', { method: 'PUT', headers, body: JSON.stringify({ ...restore, station_id: stationId }), keepalive: true }).catch(() => {});
}

async function uploadJingles(event) {
  event.preventDefault();
  const files = Array.from($('jingleFiles').files || []);
  if (!files.length) return;
  setBusy(true, 'Uploading jingles…', `${files.length} file${files.length === 1 ? '' : 's'}; waiting for library verification`);
  setResult('jingleResult');
  try {
    const form = new FormData();
    form.append('station_id', String(state.stationId)); form.append('target_station_id', String(state.stationId)); form.append('track_type', 'jingle'); form.append('auto_trim_silence', 'false'); form.append('auto_intro_clean', 'false');
    files.forEach((file) => form.append('files', file));
    const result = await api('/api/library/import/upload', { method: 'POST', body: form, timeoutMs: 120000 });
    const ids = Array.isArray(result?.imported_track_ids) ? result.imported_track_ids.map(Number) : [];
    if (!ids.length && Number(result?.scan?.added || 0) <= 0) {
      const failures = (result?.failed || []).map((item) => `${item.file}: ${item.error}`).join('; ');
      throw new Error(failures || 'No new jingle was imported');
    }
    await poll(async () => {
      const payload = await api(`/api/tracks?station_id=${state.stationId}&track_type=jingle&per_page=100`);
      state.jingles = payload.tracks || payload.items || [];
      const verified = ids.length ? ids.every((id) => state.jingles.some((track) => Number(track.id) === id)) : state.jingles.length >= Number(result.scan.added);
      return { verified, value: state.jingles };
    }, { attempts: 20, interval: 400, description: 'uploaded jingles in station library' });
    $('jingleFiles').value = ''; $('jingleFileLabel').textContent = 'Choose one or more jingle files';
    await Promise.all([loadJingles(), loadCoreStatus()]);
    const failedCount = Array.isArray(result.failed) ? result.failed.length : 0;
    const message = `Verified: ${ids.length || result.scan.added} jingle(s) imported${failedCount ? `; ${failedCount} file(s) failed` : ''}.`;
    setResult('jingleResult', message, failedCount ? 'error' : 'success'); logActivity(message, failedCount ? 'error' : 'success'); toast(message, failedCount ? 'error' : 'success');
  } catch (error) {
    const message = errorMessage(error); setResult('jingleResult', message, 'error'); logActivity(`Jingle upload failed: ${message}`, 'error'); toast(message, 'error');
  } finally { setBusy(false); }
}

async function saveSweeper(event) {
  event.preventDefault();
  const enabled = $('sweeperEnabled').checked;
  const interval = Number($('sweeperInterval').value);
  const mode = $('sweeperMode').value;
  if (!Number.isInteger(interval) || interval < 1 || interval > 100) {
    setResult('sweeperResult', 'Enter a whole number from 1 to 100 songs.', 'error');
    return;
  }
  if (!['ordered', 'random'].includes(mode)) {
    setResult('sweeperResult', 'Choose library order or stable shuffled jingle selection.', 'error');
    return;
  }
  const readVerifiedSweeper = async () => {
    try {
      const saved = await api(`/api/sweeper/config?station_id=${state.stationId}`, { timeoutMs: 8000 });
      const expectedEnabled = enabled && Number(saved.jingle_count) > 0;
      return {
        verified: Boolean(saved.enabled) === expectedEnabled
          && Number(saved.interval) === interval
          && saved.interval_unit === 'tracks'
          && saved.mode === mode,
        value: saved,
      };
    } catch (_) {
      return { verified: false };
    }
  };
  setBusy(true, 'Saving automatic jingles…', 'Saving and verifying station automation');
  setResult('sweeperResult');
  try {
    const result = await api('/api/sweeper/config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ station_id: state.stationId, enabled, interval, interval_unit: 'tracks', mode }),
      timeoutMs: 60000,
      idempotent: true,
      transportAttempts: 2,
    });
    state.sweeper = await poll(readVerifiedSweeper, { attempts: 12, interval: 300, description: 'automatic jingle setting' });
    clearFormDirty(['sweeperEnabled', 'sweeperInterval', 'sweeperMode']);
    renderCoreStatus();
    if (enabled && result?.reason === 'no_jingles') throw new Error('Cannot enable automatic jingles because this station has no jingle files');
    const message = `Verified: automatic jingles ${state.sweeper.enabled ? `play after every ${interval} completed song${interval === 1 ? '' : 's'} using ${mode === 'ordered' ? 'library order' : 'stable shuffled order'}` : 'are disabled'}.`;
    setResult('sweeperResult', message, 'success'); logActivity(message); toast(message);
  } catch (error) {
    try {
      state.sweeper = await poll(readVerifiedSweeper, {
        attempts: 45,
        interval: 1000,
        description: 'delayed automatic jingle setting',
      });
      clearFormDirty(['sweeperEnabled', 'sweeperInterval', 'sweeperMode']);
      renderCoreStatus();
      const message = `Verified after a delayed backend response: automatic jingles ${state.sweeper.enabled ? `play after every ${interval} completed song${interval === 1 ? '' : 's'} using ${mode === 'ordered' ? 'library order' : 'stable shuffled order'}` : 'are disabled'}.`;
      setResult('sweeperResult', message, 'success'); logActivity(message); toast(message);
    } catch (_) {
      const message = errorMessage(error); setResult('sweeperResult', message, 'error'); logActivity(`Automatic jingle change failed: ${message}`, 'error'); toast(message, 'error');
    }
  } finally { setBusy(false); }
}

async function saveCampaign(event) {
  event.preventDefault();
  setBusy(true, 'Saving no-copyright campaign…', 'Verifying dates, stations, voting, and AI policy');
  setResult('campaignResult');
  try {
    const startsAt = new Date($('campaignStartsAt').value);
    const endsAt = new Date($('campaignEndsAt').value);
    if (Number.isNaN(startsAt.getTime()) || Number.isNaN(endsAt.getTime())) throw new Error('Valid campaign start and end times are required.');
    state.campaign = await api('/api/campaign', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        name: $('campaignName').value.trim(),
        starts_at: startsAt.toISOString(),
        ends_at: endsAt.toISOString(),
        enabled: $('campaignEnabled').checked,
        voting_enabled: $('campaignVotingEnabled').checked,
        ai_enabled: $('campaignAiEnabled').checked,
      }),
      idempotent: true,
    });
    ['campaignName', 'campaignStartsAt', 'campaignEndsAt', 'campaignEnabled', 'campaignVotingEnabled', 'campaignAiEnabled']
      .forEach((id) => { delete $(id).dataset.dirty; });
    renderCampaign();
    const message = 'Campaign saved. Genre voting and AI are restricted to verified YouTube-playlist tracks during the campaign window.';
    setResult('campaignResult', message, 'success'); logActivity(message); toast(message);
  } catch (error) {
    const message = errorMessage(error);
    setResult('campaignResult', message, 'error'); logActivity(`Campaign save failed: ${message}`, 'error'); toast(message, 'error');
  } finally { setBusy(false); }
}

async function normalizeCampaignNames(dryRun) {
  setBusy(true, dryRun ? 'Previewing clean track names…' : 'Applying clean track names…', 'Original source names remain in compliance metadata');
  setResult('campaignResult');
  try {
    const result = await api('/api/campaign/normalize-track-names', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ dry_run: Boolean(dryRun) }),
      idempotent: true,
      timeoutMs: 30000,
    });
    const sample = (result.samples || []).slice(0, 3).map((item) => item.after).join(' · ');
    const message = `${dryRun ? 'Preview' : 'Applied'}: ${Number(result.changed || 0)} of ${Number(result.eligible || 0)} eligible names ${dryRun ? 'would change' : 'changed'}.${sample ? ` ${sample}` : ''}`;
    setResult('campaignResult', message, 'success'); logActivity(message); toast(message);
  } catch (error) {
    const message = errorMessage(error);
    setResult('campaignResult', message, 'error'); logActivity(`Campaign naming failed: ${message}`, 'error'); toast(message, 'error');
  } finally { setBusy(false); }
}

async function saveIntegrations(event) {
  event.preventDefault();
  setBusy(true, 'Saving RadioTEDU adapters…', 'Securing integration settings');
  setResult('integrationResult');
  const payload = {
    voting_enabled: $('votingEnabled').checked,
    voting_base_url: $('votingBaseUrl').value.trim(),
    voting_agent_device_id: $('votingDeviceId').value.trim(),
    voting_agent_token: $('votingAgentToken').value,
    study_enabled: $('studyEnabled').checked,
    study_base_url: $('studyBaseUrl').value.trim(),
  };
  try {
    await api('/api/integrations/radiotedu', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
      idempotent: true,
    });
    state.integrations = await api('/api/integrations/radiotedu');
    renderIntegrations();
    const message = 'Verified: optional RadioTEDU integration settings were saved securely.';
    setResult('integrationResult', message, 'success'); logActivity(message); toast(message);
  } catch (error) {
    const message = errorMessage(error);
    setResult('integrationResult', message, 'error'); logActivity(`Integration save failed: ${message}`, 'error'); toast(message, 'error');
  } finally { setBusy(false); }
}

async function testIntegrations() {
  setBusy(true, 'Testing optional services…', 'Core playout remains independent');
  setResult('integrationResult');
  try {
    const status = await api('/api/integrations/radiotedu/status', { timeoutMs: 12000 });
    const votingState = status.voting?.state || 'disabled';
    const studyState = status.study?.state || 'disabled';
    const degraded = votingState === 'degraded';
    const message = `Voting: ${votingState}. Study: ${studyState}. Core playout is unaffected.`;
    $('integrationState').textContent = degraded ? 'Degraded' : 'Ready';
    setResult('integrationResult', message, degraded ? 'error' : 'success');
    logActivity(message, degraded ? 'error' : 'success');
  } catch (error) {
    const message = `${errorMessage(error)} Core playout is unaffected.`;
    $('integrationState').textContent = 'Degraded';
    setResult('integrationResult', message, 'error'); logActivity(message, 'error');
  } finally { setBusy(false); }
}

async function saveRadioTEDUServices(event) {
  event.preventDefault();
  setBusy(true, 'Saving managed services…', 'Validating fixed commands, paths, and health endpoints');
  setResult('serviceControlResult');
  try {
    const result = await api('/api/integrations/radiotedu/services', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ services: collectRadioTEDUServiceSettings() }),
      idempotent: true,
      timeoutMs: 20000,
    });
    state.radioteduServices = result;
    (result.definitions || []).forEach((definition) => {
      ['enabled', 'autostart', 'source', 'config', 'health', 'backup'].forEach((field) => {
        const node = $(serviceControlId(definition.id, field));
        if (node) delete node.dataset.dirty;
      });
    });
    renderRadioTEDUServices();
    const message = 'Verified: managed service settings were saved. No production service was started.';
    setResult('serviceControlResult', message, 'success');
    logActivity(message);
    toast(message);
  } catch (error) {
    const message = errorMessage(error);
    setResult('serviceControlResult', message, 'error');
    logActivity(`Managed service settings failed: ${message}`, 'error');
    toast(message, 'error');
  } finally {
    setBusy(false);
  }
}

async function checkAllRadioTEDUServices() {
  setBusy(true, 'Checking RadioTEDU services…', 'Testing health, source state, and managed runtime');
  setResult('serviceControlResult');
  try {
    state.radioteduServices = await api('/api/integrations/radiotedu/services?refresh_health=true', { timeoutMs: 35000 });
    renderRadioTEDUServices();
    const statuses = state.radioteduServices.status || [];
    const healthy = statuses.filter((item) => item.state === 'healthy').length;
    const enabled = statuses.filter((item) => item.enabled).length;
    const message = `Health check complete: ${healthy} of ${enabled} enabled services are healthy. Core playout was not changed.`;
    setResult('serviceControlResult', message, healthy === enabled ? 'success' : 'error');
    logActivity(message, healthy === enabled ? 'success' : 'error');
  } catch (error) {
    const message = errorMessage(error);
    setResult('serviceControlResult', message, 'error');
    logActivity(`Service health check failed: ${message}`, 'error');
  } finally {
    setBusy(false);
  }
}

function clearServiceActionArm(button) {
  const key = `${button.dataset.serviceId}:${button.dataset.serviceAction}`;
  delete state.serviceActionArmed[key];
  button.classList.remove('armed');
  button.textContent = button.dataset.originalLabel || button.textContent;
}

async function controlRadioTEDUService(button) {
  const serviceId = button.dataset.serviceId;
  const action = button.dataset.serviceAction;
  if (!serviceId || !action) return;
  if (action === 'check') {
    setBusy(true, 'Checking service…', 'Reading health without changing runtime');
    setResult('serviceControlResult');
    try {
      const result = await api(`/api/integrations/radiotedu/services/${encodeURIComponent(serviceId)}/action`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action: 'check', confirmation: '' }),
        timeoutMs: 20000,
      });
      state.radioteduServices.status = result.status || [];
      renderRadioTEDUServices();
      const service = result.service || {};
      const message = `${service.name || serviceId}: ${String(service.state || 'checked').replaceAll('_', ' ')}. Runtime was not changed.`;
      setResult('serviceControlResult', message, service.state === 'healthy' || service.state === 'disabled' ? 'success' : 'error');
      logActivity(message);
    } catch (error) {
      const message = errorMessage(error);
      setResult('serviceControlResult', message, 'error');
      logActivity(`Service check failed: ${message}`, 'error');
    } finally {
      setBusy(false);
    }
    return;
  }
  const confirmations = {
    start: 'START SERVICE',
    stop: 'STOP SERVICE',
    restart: 'RESTART SERVICE',
    update_database: 'UPDATE DATABASE',
    update_repository: 'UPDATE REPOSITORY',
    pull_model: 'INSTALL MODEL',
  };
  const key = `${serviceId}:${action}`;
  const now = Date.now();
  if (!state.serviceActionArmed[key] || state.serviceActionArmed[key] < now) {
    Object.keys(state.serviceActionArmed).forEach((armedKey) => { delete state.serviceActionArmed[armedKey]; });
    document.querySelectorAll('[data-service-action].armed').forEach(clearServiceActionArm);
    state.serviceActionArmed[key] = now + 20000;
    button.dataset.originalLabel = button.textContent;
    button.classList.add('armed');
    button.textContent = `Confirm ${button.textContent}`;
    setResult('serviceControlResult', `Click “${button.textContent}” again within 20 seconds.`, 'error');
    window.setTimeout(() => {
      if ((state.serviceActionArmed[key] || 0) <= Date.now()) clearServiceActionArm(button);
    }, 20500);
    return;
  }
  clearServiceActionArm(button);
  setBusy(true, `${action.replaceAll('_', ' ')}…`, `Executing fixed ${serviceId} control`);
  setResult('serviceControlResult');
  try {
    const result = await api(`/api/integrations/radiotedu/services/${encodeURIComponent(serviceId)}/action`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        action,
        confirmation: confirmations[action],
        model: action === 'pull_model' ? $(serviceControlId(serviceId, 'model'))?.value.trim() || '' : '',
      }),
      timeoutMs: ['update_database', 'pull_model'].includes(action) ? 1800000 : action === 'update_repository' ? 240000 : 60000,
      idempotent: !['update_database', 'update_repository', 'pull_model'].includes(action),
    });
    state.radioteduServices.status = result.status || [];
    renderRadioTEDUServices();
    const maintenance = action === 'update_database'
      ? result.backup_file
        ? ` Backup: ${result.backup_file}. ${Number(result.migrations_applied || 0)} migration task(s) applied.`
        : ` ${Array.isArray(result.stations) ? result.stations.length : 0} station database(s) backed up and updated.`
      : '';
    const message = `Verified: ${serviceId} ${action.replaceAll('_', ' ')} completed.${maintenance}`;
    setResult('serviceControlResult', message, 'success');
    logActivity(message);
    toast(message);
  } catch (error) {
    const message = errorMessage(error);
    setResult('serviceControlResult', message, 'error');
    logActivity(`${serviceId} ${action} failed: ${message}`, 'error');
    toast(message, 'error');
  } finally {
    setBusy(false);
  }
}

async function publishVotingRound() {
  setBusy(true, 'Opening genre voting round…', 'Only campaign-eligible genres and tracks can win');
  setResult('campaignResult');
  try {
    const result = await api('/api/campaign/voting/round', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ duration_seconds: 45 }),
      timeoutMs: 15000,
    });
    state.campaign = await api('/api/campaign');
    renderCampaign();
    const message = `Genre voting round ${result.id} is open for 45 seconds.`;
    setResult('campaignResult', message, 'success'); logActivity(message); toast(message);
  } catch (error) {
    const message = `${errorMessage(error)} Playout was not changed.`;
    setResult('campaignResult', message, 'error'); logActivity(message, 'error'); toast(message, 'error');
  } finally { setBusy(false); }
}

async function resolveVotingRound() {
  setBusy(true, 'Resolving genre vote…', 'Selecting one verified eligible track for the winning genre');
  setResult('campaignResult');
  try {
    const result = await api('/api/campaign/voting/resolve', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ force: true }),
      timeoutMs: 15000,
    });
    state.campaign = await api('/api/campaign');
    renderCampaign();
    const message = `${String(result.winning_genre || '').toUpperCase()} won. Queued ${result.track_artist} - ${result.track_title} on station ${result.station_id}.`;
    setResult('campaignResult', message, 'success'); logActivity(message); toast(message);
  } catch (error) {
    const message = `${errorMessage(error)} Playout was not changed.`;
    setResult('campaignResult', message, 'error'); logActivity(message, 'error'); toast(message, 'error');
  } finally { setBusy(false); }
}

function selectedShow() {
  return state.shows.find((show) => Number(show.id) === Number(state.selectedShowId)) || null;
}

function resetShowDeleteArm() {
  state.showDeleteArmedUntil = 0;
  const button = $('deleteShowButton');
  button.classList.remove('armed');
  button.textContent = 'Delete show';
}

function renderShowEditor() {
  const show = selectedShow();
  $('showCount').textContent = String(state.shows.length);
  $('showSelect').innerHTML = '<option value="0">Create a new show</option>' + state.shows.map((item) =>
    `<option value="${Number(item.id)}">${escapeHtml(item.name)}</option>`).join('');
  $('showSelect').value = String(show?.id || 0);
  $('showName').value = show?.name || '';
  $('showDescription').value = show?.description || '';
  $('showColor').value = /^#[0-9a-f]{6}$/i.test(String(show?.color || '')) ? show.color : '#4a90d9';
  $('showActive').checked = show ? Number(show.is_active) !== 0 : true;
  $('saveShowButton').textContent = show ? 'Save and verify show' : 'Create show';
  $('deleteShowButton').hidden = !show;
  $('showSelectedState').textContent = show?.name || 'New show';
  const session = state.showSession?.session || state.showSession || null;
  const status = String(session?.status || 'not_live').replaceAll('_', ' ');
  $('showSessionState').textContent = session ? status : 'Not live';
  const ownsSession = show && Number(session?.show_id || 0) === Number(show.id);
  $('showGoLiveButton').disabled = !show || Boolean(session);
  $('showGoBreakButton').disabled = !ownsSession || !['live', 'break_intro'].includes(String(session?.status || ''));
  $('showEndButton').disabled = !ownsSession || !['live', 'on_break', 'break_outro', 'break_intro'].includes(String(session?.status || ''));
  resetShowDeleteArm();
}

function renderShowAssignments() {
  const show = selectedShow();
  $('showAssignmentCount').textContent = String(state.showAssignments.length);
  $('showAssignmentUser').innerHTML = '<option value="">Select an operator</option>' + state.showCandidates.map((user) =>
    `<option value="${Number(user.id)}">${escapeHtml(user.display_name || user.username || `Operator ${user.id}`)}</option>`).join('');
  ['showAssignmentUser', 'showAssignmentRole', 'assignShowUserButton', 'showAudioType', 'showAudioFile', 'uploadShowAudioButton'].forEach((id) => { $(id).disabled = !show; });
  $('showAssignmentList').innerHTML = !show
    ? '<div class="empty-state">Select a saved show to manage assignments.</div>'
    : state.showAssignments.length
      ? state.showAssignments.map((assignment) => {
        const userId = Number(assignment.user_id || assignment.id || 0);
        const label = assignment.display_name || assignment.username || `Operator ${userId}`;
        const permissions = Array.isArray(assignment.permission_keys) ? assignment.permission_keys.join(', ') : '';
        return `<div class="record-row"><div class="record-copy"><b>${escapeHtml(label)}</b><span>${escapeHtml(assignment.role || 'dj')}</span><small>${escapeHtml(permissions || 'Default show-role permissions')}</small></div><button class="button danger compact" type="button" data-show-unassign="${userId}">Unassign</button></div>`;
      }).join('')
      : '<div class="empty-state">No operators are assigned to this show.</div>';
}

async function loadShowAssignments() {
  const show = selectedShow();
  if (!show) {
    state.showAssignments = [];
    state.showCandidates = [];
    renderShowAssignments();
    return;
  }
  const [assignments, candidates] = await Promise.all([
    api(`/api/shows/${show.id}/assignments`),
    api(`/api/shows/${show.id}/assignment-candidates`),
  ]);
  state.showAssignments = Array.isArray(assignments) ? assignments : (assignments?.items || []);
  state.showCandidates = Array.isArray(candidates) ? candidates : (candidates?.items || []);
  renderShowAssignments();
}

async function loadShows(preferredShowId = null) {
  const [shows, sessionPayload] = await Promise.all([
    api(`/api/shows/?station_id=${state.stationId}`),
    api(`/api/shows/session/current?station_id=${state.stationId}`).catch((error) => error.status === 404 ? { session: null } : Promise.reject(error)),
  ]);
  state.shows = Array.isArray(shows) ? shows : (shows?.items || []);
  const requested = Number(preferredShowId || state.selectedShowId || 0);
  state.selectedShowId = state.shows.some((show) => Number(show.id) === requested) ? requested : 0;
  state.showSession = sessionPayload?.session || null;
  renderShowEditor();
  await loadShowAssignments();
}

async function selectShow() {
  state.selectedShowId = Number($('showSelect').value || 0);
  state.showAssignments = [];
  state.showCandidates = [];
  setResult('showResult');
  setResult('showAssignmentResult');
  renderShowEditor();
  await loadShowAssignments();
}

async function saveShow(event) {
  event.preventDefault();
  const existing = selectedShow();
  setBusy(true, existing ? 'Saving show…' : 'Creating show…', 'Writing and reading back program configuration');
  setResult('showResult');
  try {
    const body = {
      name: $('showName').value.trim(),
      description: $('showDescription').value.trim(),
      color: $('showColor').value,
      is_active: $('showActive').checked ? 1 : 0,
    };
    if (!existing) body.station_id = state.stationId;
    const saved = await api(existing ? `/api/shows/${existing.id}` : '/api/shows/', {
      method: existing ? 'PUT' : 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
      idempotent: Boolean(existing),
    });
    await loadShows(Number(saved?.id || existing?.id || 0));
    const verified = selectedShow();
    if (!verified || verified.name !== body.name || Number(verified.is_active) !== Number(body.is_active)) throw new Error('Show read-back did not match the saved configuration');
    const message = `Verified show: ${verified.name}.`;
    setResult('showResult', message, 'success'); logActivity(message); toast(message);
  } catch (error) {
    const message = errorMessage(error); setResult('showResult', message, 'error'); logActivity(`Show save failed: ${message}`, 'error');
  } finally { setBusy(false); }
}

async function deleteShow() {
  const show = selectedShow();
  if (!show) return;
  if (state.showDeleteArmedUntil < Date.now()) {
    state.showDeleteArmedUntil = Date.now() + 20000;
    $('deleteShowButton').classList.add('armed');
    $('deleteShowButton').textContent = 'Confirm delete show';
    setResult('showResult', 'Click “Confirm delete show” again within 20 seconds. Live station audio is not changed.', 'error');
    window.setTimeout(() => { if (state.showDeleteArmedUntil <= Date.now()) resetShowDeleteArm(); }, 20500);
    return;
  }
  setBusy(true, 'Deleting show…', 'Removing only the selected show record');
  try {
    await api(`/api/shows/${show.id}`, { method: 'DELETE', idempotent: true });
    state.selectedShowId = 0;
    await loadShows();
    const message = `Deleted show ${show.name}. Station broadcasting was not changed.`;
    setResult('showResult', message, 'success'); logActivity(message); toast(message);
  } catch (error) { setResult('showResult', errorMessage(error), 'error'); }
  finally { setBusy(false); }
}

async function runShowAction(action) {
  const show = selectedShow();
  if (!show) return;
  const endpoints = {
    live: `/api/shows/${show.id}/go-live`,
    break: `/api/shows/${show.id}/go-break`,
    end: `/api/shows/${show.id}/end`,
  };
  setBusy(true, `${action === 'live' ? 'Starting' : action === 'break' ? 'Sending' : 'Ending'} show…`, 'Waiting for verified show-session state');
  setResult('showResult');
  try {
    const options = { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: action === 'live' ? JSON.stringify({ station_id: state.stationId }) : '{}' };
    await api(endpoints[action], options);
    await loadShows(show.id);
    const message = `Verified show action: ${action}.`;
    setResult('showResult', message, 'success'); logActivity(`${show.name}: ${message}`); toast(message);
  } catch (error) { const message = errorMessage(error); setResult('showResult', message, 'error'); logActivity(`Show action failed: ${message}`, 'error'); }
  finally { setBusy(false); }
}

async function assignShowUser(event) {
  event.preventDefault();
  const show = selectedShow();
  const userId = Number($('showAssignmentUser').value || 0);
  if (!show || !userId) { setResult('showAssignmentResult', 'Select a saved show and an operator.', 'error'); return; }
  setBusy(true, 'Assigning operator…', 'Saving and reading back show permissions');
  try {
    await api(`/api/shows/${show.id}/assign`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ user_id: userId, role: $('showAssignmentRole').value }) });
    await loadShowAssignments();
    if (!state.showAssignments.some((item) => Number(item.user_id || item.id) === userId)) throw new Error('Assignment read-back did not contain the selected operator');
    setResult('showAssignmentResult', 'Operator assignment verified.', 'success');
  } catch (error) { setResult('showAssignmentResult', errorMessage(error), 'error'); }
  finally { setBusy(false); }
}

async function unassignShowUser(userId) {
  const show = selectedShow();
  if (!show || !userId) return;
  setBusy(true, 'Removing assignment…', 'The operator account itself is preserved');
  try {
    await api(`/api/shows/${show.id}/assign/${userId}`, { method: 'DELETE', idempotent: true });
    await loadShowAssignments();
    setResult('showAssignmentResult', 'Show assignment removed and verified.', 'success');
  } catch (error) { setResult('showAssignmentResult', errorMessage(error), 'error'); }
  finally { setBusy(false); }
}

async function uploadShowAudio(event) {
  event.preventDefault();
  const show = selectedShow();
  const file = $('showAudioFile').files[0];
  if (!show || !file) return;
  const body = new FormData(); body.append('type', $('showAudioType').value); body.append('file', file);
  setBusy(true, 'Uploading show audio…', 'Sanitizing, storing, and reading back transition audio');
  try {
    const result = await api(`/api/shows/${show.id}/upload-audio`, { method: 'POST', body, timeoutMs: 120000 });
    await loadShows(show.id); $('showAudioFile').value = '';
    setResult('showAssignmentResult', `Verified ${String(result.type || 'show').replaceAll('_', ' ')} audio.`, 'success');
  } catch (error) { setResult('showAssignmentResult', errorMessage(error), 'error'); }
  finally { setBusy(false); }
}

function inclusiveMusicUsageEndDate() {
  const value = $('musicUsageTo').value;
  if (!value) return '';
  const date = new Date(`${value}T00:00:00Z`); date.setUTCDate(date.getUTCDate() + 1);
  return date.toISOString().slice(0, 10);
}

function musicUsageQuery() {
  const query = new URLSearchParams({ station_id: String(state.stationId), limit: '1000' });
  if ($('musicUsageIncludeJuke').checked) query.set('include_juke', 'true');
  if ($('musicUsageFrom').value) query.set('date_from', $('musicUsageFrom').value);
  const end = inclusiveMusicUsageEndDate(); if (end) query.set('date_to', end);
  return query;
}

function renderMusicUsage() {
  $('musicUsageCount').textContent = String(state.musicUsage.length);
  $('musicUsageList').innerHTML = state.musicUsage.length ? state.musicUsage.map((entry) => {
    const creators = [entry.composer && `Composer: ${entry.composer}`, entry.lyricist && `Lyrics: ${entry.lyricist}`, entry.label && `Label: ${entry.label}`].filter(Boolean).join(' · ');
    const durations = `${formatDuration(entry.played_duration_seconds)} played / ${formatDuration(entry.scheduled_duration_seconds)} scheduled`;
    const trackId = Number(entry.track_id || 0);
    const sourceLabel = entry.source_system === 'juke_local' ? 'Juke Local · verified ledger' : 'RadioTEDU OnAir';
    const metadataButton = trackId > 0 ? `<button class="button ghost compact" type="button" data-music-track="${trackId}">Metadata</button>` : '';
    return `<div class="record-row"><div class="record-copy"><b>${escapeHtml(entry.performer || 'Unknown artist')} - ${escapeHtml(entry.work_title || 'Untitled')}</b><span>${escapeHtml(entry.version || 'Version not documented')} · ${escapeHtml(entry.isrc || 'No ISRC')}</span><small>${escapeHtml(creators || 'Creator and label details not yet documented')}</small><small>${escapeHtml(entry.source_reference || entry.source_path || 'Source not documented')}</small></div><div class="record-meta"><b>${escapeHtml(String(entry.broadcast_at || ''))}</b><span>${escapeHtml(durations)}</span><span>${escapeHtml(sourceLabel)}</span><span>Log ${escapeHtml(entry.log_id || '')}</span>${metadataButton}</div></div>`;
  }).join('') : '<div class="empty-state">No completed music plays match this period.</div>';
}

async function loadMusicUsage() {
  const payload = await api(`/api/music-usage?${musicUsageQuery()}`);
  state.musicUsage = Array.isArray(payload) ? payload : (payload?.items || []);
  renderMusicUsage();
  const juke = payload?.sources?.juke_local;
  const jukeText = juke ? ` Juke ledger: ${juke.integrity_ok ? 'verified' : 'not verified'}, ${Number(juke.record_count || 0)} total record(s).` : '';
  setResult('musicUsageResult', `Loaded ${state.musicUsage.length} permanent record(s).${jukeText}`, 'success');
}

async function exportMusicUsage() {
  setBusy(true, 'Preparing CSV…', 'Exporting the filtered hash-chained ledger without changing it');
  try {
    const query = musicUsageQuery(); query.delete('limit'); query.set('format', 'csv');
    const csv = await api(`/api/music-usage/export?${query}`);
    const blob = new Blob([String(csv || '')], { type: 'text/csv;charset=utf-8' });
    const href = URL.createObjectURL(blob); const link = document.createElement('a');
    link.href = href; link.download = `RadioTEDU-music-usage-station-${state.stationId}-${$('musicUsageFrom').value || 'all'}-${$('musicUsageTo').value || 'latest'}.csv`;
    document.body.appendChild(link); link.click(); link.remove(); URL.revokeObjectURL(href);
    setResult('musicUsageResult', 'CSV prepared for Excel and downloaded.', 'success'); logActivity('Music-use CSV exported.');
  } catch (error) { setResult('musicUsageResult', errorMessage(error), 'error'); }
  finally { setBusy(false); }
}

function fillMusicMetadata(metadata) {
  $('musicMetadataTrackId').value = String(metadata.track_id || $('musicMetadataTrackId').value || '');
  $('musicMetadataVersion').value = metadata.version || '';
  $('musicMetadataIsrc').value = metadata.isrc || '';
  $('musicMetadataComposer').value = metadata.composer || '';
  $('musicMetadataLyricist').value = metadata.lyricist || '';
  $('musicMetadataProducer').value = metadata.phonogram_producer || '';
  $('musicMetadataLabel').value = metadata.label || '';
  $('musicMetadataSource').value = metadata.source_reference || '';
  $('musicMetadataSourceType').value = metadata.source_type || '';
  $('musicMetadataRights').value = metadata.rights_reference || '';
  $('musicMetadataNotes').value = metadata.notes || '';
  $('musicMetadataState').textContent = metadata.track_id ? `Track ${metadata.track_id}` : 'Enter track ID';
}

async function loadMusicMetadata(trackId = null) {
  const id = Number(trackId || $('musicMetadataTrackId').value || 0);
  if (!id) { setResult('musicMetadataResult', 'Enter a track ID.', 'error'); return; }
  const metadata = await api(`/api/music-usage/track-metadata/${id}`);
  fillMusicMetadata(metadata || { track_id: id });
  setResult('musicMetadataResult', `Loaded documentation for track ${id}.`, 'success');
}

async function saveMusicMetadata(event) {
  event.preventDefault();
  const id = Number($('musicMetadataTrackId').value || 0);
  const body = {
    version: $('musicMetadataVersion').value.trim(), composer: $('musicMetadataComposer').value.trim(), lyricist: $('musicMetadataLyricist').value.trim(),
    phonogram_producer: $('musicMetadataProducer').value.trim(), label: $('musicMetadataLabel').value.trim(), isrc: $('musicMetadataIsrc').value.trim(),
    source_reference: $('musicMetadataSource').value.trim(), source_type: $('musicMetadataSourceType').value.trim(), rights_reference: $('musicMetadataRights').value.trim(), notes: $('musicMetadataNotes').value.trim(),
  };
  setBusy(true, 'Saving rights metadata…', 'Persisting and reading back track documentation');
  try {
    await api(`/api/music-usage/track-metadata/${id}`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body), idempotent: true });
    const verified = await api(`/api/music-usage/track-metadata/${id}`); fillMusicMetadata(verified);
    if (String(verified.version || '') !== body.version || String(verified.isrc || '') !== body.isrc.toUpperCase()) throw new Error('Metadata read-back did not match the saved values');
    setResult('musicMetadataResult', `Track ${id} metadata saved and verified.`, 'success'); logActivity(`Track ${id} compliance metadata updated.`);
  } catch (error) { setResult('musicMetadataResult', errorMessage(error), 'error'); }
  finally { setBusy(false); }
}

function renderMusicClosures() {
  $('musicClosureCount').textContent = String(state.musicClosures.length);
  $('musicClosureList').innerHTML = state.musicClosures.length ? state.musicClosures.map((item) =>
    `<div class="record-row"><div class="record-copy"><b>${escapeHtml(item.period_key || `${item.period_start || ''} - ${item.period_end || ''}`)}</b><span>${Number(item.record_count || 0)} record(s) · closed by ${escapeHtml(item.closed_by || 'operator')}</span><small>${escapeHtml(item.export_path || '')}</small></div><div class="record-meta"><span>SHA-256</span><small>${escapeHtml(String(item.checksum || '').slice(0, 16))}…</small></div></div>`).join('')
    : '<div class="empty-state">No monthly closures have been created.</div>';
}

async function loadMusicClosures() {
  const payload = await api('/api/music-usage/monthly-closures?limit=24');
  state.musicClosures = Array.isArray(payload) ? payload : (payload?.items || []);
  renderMusicClosures();
}

async function closeMusicMonth(event) {
  event.preventDefault();
  const year = Number($('musicCloseYear').value); const month = Number($('musicCloseMonth').value);
  setBusy(true, 'Closing reporting month…', 'Creating or verifying the retained CSV and hash summary');
  setResult('musicClosureResult');
  try {
    const result = await api('/api/music-usage/monthly-close', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ year, month, closed_by: currentUser().username || 'operator', include_juke: $('musicUsageIncludeJuke').checked }), idempotent: true });
    await loadMusicClosures();
    const message = `Verified ${result.period_key || `${year}-${String(month).padStart(2, '0')}`}: ${Number(result.record_count || 0)} record(s), checksum retained.`;
    setResult('musicClosureResult', message, 'success'); logActivity(message); toast(message);
  } catch (error) { setResult('musicClosureResult', errorMessage(error), 'error'); }
  finally { setBusy(false); }
}

function renderYtdlpJobs() {
  const settings = state.ytdlpSettings || {};
  $('ytdlpDependencyState').textContent = settings.binary_found && settings.ffmpeg_found ? 'yt-dlp + FFmpeg ready' : 'Dependencies missing';
  const jobs = state.ytdlpJobs || {};
  const rows = [jobs.running, ...(jobs.queue || []), ...(jobs.recent || [])].filter(Boolean);
  $('ytdlpJobList').innerHTML = rows.length ? rows.map((job) => {
    const result = job.result || {};
    const detail = job.error || job.message || (result.downloaded_files !== undefined ? `${Number(result.downloaded_files)} file(s) downloaded` : job.phase || 'queued');
    return `<div class="record-row"><div class="record-copy"><b>${escapeHtml(String(job.track_type || 'music').toUpperCase())} import</b><span>${escapeHtml(job.url || '')}</span><small>${escapeHtml(detail)}</small></div><div class="record-meta"><span>${escapeHtml(job.status || 'queued')}</span><small>${escapeHtml(job.updated_at || job.created_at || '')}</small></div></div>`;
  }).join('') : '<div class="empty-state">No download jobs are queued or retained in recent history.</div>';
}

function scheduleYtdlpPolling() {
  if (state.ytdlpPollTimer) window.clearInterval(state.ytdlpPollTimer);
  state.ytdlpPollTimer = null;
  const jobs = state.ytdlpJobs || {};
  if (!jobs.running && !(jobs.queue || []).length) return;
  state.ytdlpPollTimer = window.setInterval(() => {
    if (state.activeView !== 'media' || document.hidden) return;
    loadYtdlpJobs().catch((error) => setResult('ytdlpResult', errorMessage(error), 'error'));
  }, 5000);
}

async function loadYtdlpJobs() {
  state.ytdlpJobs = await api('/api/library/import/ytdlp/jobs/status?limit_recent=25');
  renderYtdlpJobs();
  scheduleYtdlpPolling();
}

async function loadYtdlpWorkspace() {
  const [settings] = await Promise.all([api(`/api/library/import/ytdlp/settings?station_id=${Number(state.stationId)}`), loadYtdlpJobs()]);
  state.ytdlpSettings = settings || {};
  if ($('ytdlpAudioFormat').dataset.dirty !== '1') $('ytdlpAudioFormat').value = settings.default_audio_format || 'mp3';
  if ($('ytdlpAudioQuality').dataset.dirty !== '1') $('ytdlpAudioQuality').value = settings.default_audio_quality || '192';
  if ($('ytdlpDownloadPlaylist').dataset.dirty !== '1') $('ytdlpDownloadPlaylist').checked = Boolean(settings.default_allow_playlist);
  if ($('ytdlpMusicOnly').dataset.dirty !== '1') $('ytdlpMusicOnly').checked = Boolean(settings.default_music_only_mode);
  if ($('ytdlpAutoTrim').dataset.dirty !== '1') $('ytdlpAutoTrim').checked = Boolean(settings.default_auto_trim);
  if ($('ytdlpAutoIntro').dataset.dirty !== '1') $('ytdlpAutoIntro').checked = Boolean(settings.default_auto_intro_clean);
  renderYtdlpJobs();
}

async function queueYtdlpImport(event) {
  event.preventDefault();
  let parsed;
  try { parsed = new URL($('ytdlpUrl').value.trim()); } catch (_) { return setResult('ytdlpResult', 'Enter a valid http(s) video or playlist URL.', 'error'); }
  if (!['http:', 'https:'].includes(parsed.protocol)) return setResult('ytdlpResult', 'Only http(s) download URLs are allowed.', 'error');
  const payload = {
    url: parsed.toString(), station_id: Number(state.stationId), target_station_id: Number(state.stationId),
    track_type: $('ytdlpTrackType').value, download_playlist: $('ytdlpDownloadPlaylist').checked,
    music_only_mode: $('ytdlpMusicOnly').checked, audio_format: $('ytdlpAudioFormat').value,
    audio_quality: $('ytdlpAudioQuality').value.trim() || '192', auto_trim_silence: $('ytdlpAutoTrim').checked,
    trim_threshold_db: Number(state.ytdlpSettings?.trim_threshold_db ?? -45), trim_min_silence: Number(state.ytdlpSettings?.trim_min_silence ?? 0.15),
    auto_intro_clean: $('ytdlpAutoIntro').checked, intro_clean_preset: $('ytdlpIntroPreset').value,
    intro_max_cut_s: Number(state.ytdlpSettings?.intro_max_cut_s ?? 18),
  };
  setBusy(true, 'Queueing download...', 'Creating a durable server-side import job');
  try {
    const queued = await api('/api/library/import/ytdlp/jobs', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload), timeoutMs: 30000 });
    await loadYtdlpJobs();
    const jobId = String(queued?.job?.id || '');
    const all = [state.ytdlpJobs?.running, ...(state.ytdlpJobs?.queue || []), ...(state.ytdlpJobs?.recent || [])].filter(Boolean);
    if (!jobId || !all.some((job) => String(job.id) === jobId)) throw new Error('Download job was queued but missing from read-back');
    const message = `Verified: download job ${jobId} is queued and will continue server-side.`; setResult('ytdlpResult', message, 'success'); logActivity(message);
    $('ytdlpUrl').value = '';
  } catch (error) { const message = errorMessage(error); setResult('ytdlpResult', message, 'error'); logActivity(`Download queue failed: ${message}`, 'error'); } finally { setBusy(false); }
}

function renderMetadataRules() {
  $('metadataRuleCount').textContent = String(state.metadataRules.length);
  $('metadataRuleList').innerHTML = state.metadataRules.length ? state.metadataRules.map((rule) => {
    const id = Number(rule.id); const armed = (state.metadataRuleDeleteArmed[id] || 0) > Date.now();
    return `<div class="record-row"><div class="record-copy"><b>${escapeHtml(rule.name || `Rule ${id}`)}</b><span>${escapeHtml(rule.target_field)} ${escapeHtml(rule.match_type)} "${escapeHtml(rule.pattern)}" -> "${escapeHtml(rule.replacement)}"</span><small>${escapeHtml(rule.scope)} / priority ${Number(rule.priority || 0)} / ${rule.is_active ? 'active' : 'disabled'}</small></div><div class="row-actions"><button class="icon-button" type="button" data-metadata-rule-toggle="${id}">${rule.is_active ? 'Disable' : 'Enable'}</button><button class="icon-button remove${armed ? ' armed' : ''}" type="button" data-metadata-rule-delete="${id}">${armed ? 'Confirm delete' : 'Delete'}</button></div></div>`;
  }).join('') : '<div class="empty-state">No metadata normalization rules exist for this station.</div>';
}

async function loadMetadataRules() {
  const payload = await api(`/api/library/metadata/rules?station_id=${Number(state.stationId)}&active_only=false`);
  state.metadataRules = payload?.rules || [];
  renderMetadataRules();
}

async function createMetadataRule(event) {
  event.preventDefault();
  const payload = {
    station_id: Number(state.stationId), scope: 'station', name: $('metadataRuleName').value.trim(),
    target_field: $('metadataRuleField').value, match_type: $('metadataRuleMatch').value,
    pattern: $('metadataRulePattern').value, replacement: $('metadataRuleReplacement').value,
    is_case_sensitive: $('metadataRuleCaseSensitive').checked, priority: Number($('metadataRulePriority').value || 100), is_active: true,
  };
  if (!payload.name || !payload.pattern) return setResult('metadataRuleResult', 'Enter a rule name and pattern.', 'error');
  try {
    const created = await api('/api/library/metadata/rules', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
    await loadMetadataRules();
    const stored = state.metadataRules.find((rule) => Number(rule.id) === Number(created?.id));
    if (!stored || stored.pattern !== payload.pattern || stored.replacement !== payload.replacement) throw new Error('Created metadata rule did not match read-back');
    const message = `Verified: metadata rule "${payload.name}" was created.`; setResult('metadataRuleResult', message, 'success'); logActivity(message);
  } catch (error) { setResult('metadataRuleResult', errorMessage(error), 'error'); }
}

async function toggleMetadataRule(ruleId) {
  const id = Number(ruleId); const rule = state.metadataRules.find((item) => Number(item.id) === id); if (!rule) return;
  const expected = !Boolean(rule.is_active);
  try {
    await api(`/api/library/metadata/rules/${id}`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ is_active: expected }) });
    await loadMetadataRules();
    if (Boolean(state.metadataRules.find((item) => Number(item.id) === id)?.is_active) !== expected) throw new Error('Metadata-rule state did not match read-back');
    setResult('metadataRuleResult', `Verified: rule ${id} is ${expected ? 'enabled' : 'disabled'}.`, 'success');
  } catch (error) { setResult('metadataRuleResult', errorMessage(error), 'error'); }
}

async function deleteMetadataRule(ruleId) {
  const id = Number(ruleId); if (!state.metadataRules.some((item) => Number(item.id) === id)) return;
  if ((state.metadataRuleDeleteArmed[id] || 0) <= Date.now()) {
    state.metadataRuleDeleteArmed = { [id]: Date.now() + 20000 }; renderMetadataRules();
    setResult('metadataRuleResult', `Click "Confirm delete" for rule ${id} again within 20 seconds.`, 'error');
    window.setTimeout(() => { if ((state.metadataRuleDeleteArmed[id] || 0) <= Date.now()) { state.metadataRuleDeleteArmed = {}; renderMetadataRules(); } }, 20500); return;
  }
  state.metadataRuleDeleteArmed = {};
  try {
    await api(`/api/library/metadata/rules/${id}`, { method: 'DELETE' }); await loadMetadataRules();
    if (state.metadataRules.some((item) => Number(item.id) === id)) throw new Error('Deleted metadata rule still appeared in read-back');
    setResult('metadataRuleResult', `Verified: metadata rule ${id} was deleted.`, 'success');
  } catch (error) { setResult('metadataRuleResult', errorMessage(error), 'error'); }
}

function disarmMetadataMaintenance() {
  state.metadataMaintenanceArmedUntil = 0; $('runMetadataMaintenanceButton').classList.remove('armed'); $('runMetadataMaintenanceButton').textContent = 'Arm metadata operation';
}

async function runMetadataMaintenance(event) {
  event.preventDefault();
  if (state.metadataMaintenanceArmedUntil <= Date.now()) {
    state.metadataMaintenanceArmedUntil = Date.now() + 20000; $('runMetadataMaintenanceButton').classList.add('armed'); $('runMetadataMaintenanceButton').textContent = 'Confirm metadata operation';
    setResult('metadataMaintenanceResult', 'Click "Confirm metadata operation" again within 20 seconds.', 'error');
    window.setTimeout(() => { if (state.metadataMaintenanceArmedUntil <= Date.now()) disarmMetadataMaintenance(); }, 20500); return;
  }
  disarmMetadataMaintenance();
  const action = $('metadataMaintenanceAction').value; const limit = Number($('metadataMaintenanceLimit').value || 0); const stationId = Number(state.stationId);
  const operations = {
    normalize: ['/api/library/metadata/normalize', { station_id: stationId, analyze_bpm: true, limit, library_scope: 'local' }],
    autofix: ['/api/library/metadata/autofix', { station_id: stationId, analyze_bpm: true, limit, library_scope: 'local', auto_seed_rules: true, rule_scope: 'station', verify_with_itunes: false }],
    itunes: ['/api/library/metadata/verify/itunes', { station_id: stationId, limit, min_confidence: 0.88, country: 'TR', dry_run: $('metadataItunesDryRun').checked, library_scope: 'local', track_type: 'music' }],
    bpm: ['/api/library/bpm/analyze', { station_id: stationId, only_missing: true, track_type: 'music', limit }],
  };
  const [endpoint, payload] = operations[action] || operations.normalize;
  setBusy(true, 'Running library maintenance...', `${action} for ${selectedStationName()}`); $('metadataMaintenanceState').textContent = 'Running';
  try {
    const result = await api(endpoint, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload), timeoutMs: 1800000 });
    const summary = result?.summary || result || {}; $('metadataMaintenanceState').textContent = 'Completed';
    const message = `Verified: ${action} completed. ${JSON.stringify(summary).slice(0, 1200)}`; setResult('metadataMaintenanceResult', message, 'success'); logActivity(`${action} metadata maintenance completed.`);
    await Promise.all([loadMetadataRules(), loadLibrary(1)]);
  } catch (error) { $('metadataMaintenanceState').textContent = 'Failed'; setResult('metadataMaintenanceResult', errorMessage(error), 'error'); } finally { setBusy(false); }
}

function renderSpeakerMonitor() {
  const selectedId = Number(state.speakerMonitor?.station_id || 0);
  $('speakerMonitorStation').innerHTML = '<option value="">No station selected</option>' + state.stations.map((station) => `<option value="${Number(station.id)}">${escapeHtml(station.name)}</option>`).join('');
  $('speakerMonitorStation').value = selectedId ? String(selectedId) : '';
  $('speakerMonitorState').textContent = selectedId ? (state.speakerMonitor?.station?.name || `Station ${selectedId}`) : 'No monitor station';
}

async function loadSpeakerMonitor() {
  state.speakerMonitor = await api('/api/speaker/monitor');
  renderSpeakerMonitor();
}

async function saveSpeakerMonitor(event) {
  event.preventDefault();
  const stationId = Number($('speakerMonitorStation').value || 0);
  if (!stationId) return setResult('speakerMonitorResult', 'Choose a station for the speaker monitor.', 'error');
  try {
    await api('/api/speaker/monitor', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ station_id: stationId }) });
    await loadSpeakerMonitor();
    if (Number(state.speakerMonitor?.station_id) !== stationId) throw new Error('Speaker monitor read-back did not match the selected station');
    const message = `Verified: speaker monitor follows ${state.speakerMonitor?.station?.name || `station ${stationId}`}.`; setResult('speakerMonitorResult', message, 'success'); logActivity(message);
  } catch (error) { setResult('speakerMonitorResult', errorMessage(error), 'error'); }
}

function toggleStartupSoundFields() {
  const fixed = $('startupSoundMode').value === 'fixed';
  $('startupSoundTrack').disabled = !fixed;
}

function renderStartupSound() {
  const config = state.startupSound || {};
  $('startupSoundEnabled').checked = Boolean(config.enabled);
  $('startupSoundMode').value = config.mode === 'fixed' ? 'fixed' : 'random';
  $('startupSoundTrack').innerHTML = '<option value="0">No fixed track</option>' + (config.jingles || []).map((track) => `<option value="${Number(track.id)}">${escapeHtml(track.label || track.title || `Track ${track.id}`)}</option>`).join('');
  $('startupSoundTrack').value = String(Number(config.track_id || 0));
  $('startupSoundState').textContent = config.enabled ? (config.track_title || `${config.mode || 'random'} mode`) : 'Disabled';
  toggleStartupSoundFields();
}

async function loadStartupSound() {
  if (!state.stationId) return;
  state.startupSound = await api(`/api/startup-sound/config?station_id=${Number(state.stationId)}`);
  renderStartupSound();
}

async function saveStartupSound(event) {
  event.preventDefault();
  const payload = { station_id: Number(state.stationId), enabled: $('startupSoundEnabled').checked, mode: $('startupSoundMode').value, track_id: Number($('startupSoundTrack').value || 0) };
  if (payload.enabled && payload.mode === 'fixed' && !payload.track_id) return setResult('startupSoundResult', 'Select a fixed startup track or use deterministic rotation.', 'error');
  try {
    await api('/api/startup-sound/config', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
    await loadStartupSound();
    if (Boolean(state.startupSound?.enabled) !== payload.enabled || String(state.startupSound?.mode) !== payload.mode || Number(state.startupSound?.track_id || 0) !== payload.track_id) throw new Error('Startup-sound read-back did not match the saved configuration');
    const message = 'Verified: startup-sound configuration matches the stored station policy.'; setResult('startupSoundResult', message, 'success'); logActivity(message);
  } catch (error) { setResult('startupSoundResult', errorMessage(error), 'error'); }
}

async function uploadStartupSound(event) {
  event.preventDefault();
  const file = $('startupSoundFile').files?.[0]; if (!file || !state.stationId) return setResult('startupSoundResult', 'Select a station and an audio file.', 'error');
  const form = new FormData(); form.append('station_id', String(Number(state.stationId))); form.append('file', file, file.name);
  setBusy(true, 'Uploading startup sound...', 'Copying audio into managed storage and verifying the track');
  try {
    const created = await api('/api/startup-sound/upload', { method: 'POST', body: form, timeoutMs: 120000 });
    await loadStartupSound();
    const trackId = Number(created?.track_id || 0);
    if (!trackId || !(state.startupSound?.jingles || []).some((item) => Number(item.id) === trackId)) throw new Error('Uploaded startup audio was missing from read-back');
    $('startupSoundMode').value = 'fixed'; $('startupSoundTrack').value = String(trackId); toggleStartupSoundFields();
    const message = `Verified: startup audio "${created.title || file.name}" is stored. Save the form to activate it.`; setResult('startupSoundResult', message, 'success'); logActivity(message);
    $('startupSoundFile').value = '';
  } catch (error) { setResult('startupSoundResult', errorMessage(error), 'error'); } finally { setBusy(false); }
}

function renderPlaylistEditor() {
  $('playlistCount').textContent = String(state.playlists.length);
  $('playlistSelect').innerHTML = '<option value="0">Select a playlist</option>' + state.playlists.map((playlist) => `<option value="${Number(playlist.id)}">${escapeHtml(playlist.name)} (${Number(playlist.item_count || 0)})</option>`).join('');
  $('playlistSelect').value = String(state.selectedPlaylistId || 0);
  $('playlistItemCount').textContent = String(state.playlistItems.length);
  $('deletePlaylistButton').hidden = !state.selectedPlaylistId;
  $('deletePlaylistButton').textContent = state.playlistDeleteArmedUntil > Date.now() ? 'Confirm delete playlist' : 'Delete playlist';
  const disabled = !state.selectedPlaylistId;
  $('playlistTrackId').disabled = disabled;
  $('playlistAddItemButton').disabled = disabled;
  $('playlistBulkTrackIds').disabled = disabled;
  $('playlistBulkButton').disabled = disabled;
  $('playlistItemList').innerHTML = state.playlistItems.length ? state.playlistItems.map((item, index) => `
    <div class="record-row"><div class="record-copy"><b>${escapeHtml(item.title || `Track ${item.track_id}`)}</b><span>${escapeHtml(item.artist || 'Unknown artist')}</span><small>Track ${Number(item.track_id)} / position ${index + 1}</small></div><div class="row-actions"><button class="icon-button" type="button" data-playlist-move="up" data-playlist-item="${Number(item.id)}" ${index === 0 ? 'disabled' : ''}>Up</button><button class="icon-button" type="button" data-playlist-move="down" data-playlist-item="${Number(item.id)}" ${index === state.playlistItems.length - 1 ? 'disabled' : ''}>Down</button><button class="icon-button remove" type="button" data-playlist-remove="${Number(item.id)}">Remove</button></div></div>`).join('') : `<div class="empty-state">${state.selectedPlaylistId ? 'This playlist is empty.' : 'Select a playlist to edit its order.'}</div>`;
}

async function loadPlaylists(preferredPlaylistId = state.selectedPlaylistId) {
  if (!state.stationId) return;
  const rows = await api(`/api/playlists?station_id=${Number(state.stationId)}`);
  state.playlists = Array.isArray(rows) ? rows : [];
  state.selectedPlaylistId = state.playlists.some((item) => Number(item.id) === Number(preferredPlaylistId)) ? Number(preferredPlaylistId) : 0;
  state.playlistItems = [];
  if (state.selectedPlaylistId) {
    const detail = await api(`/api/playlists/${state.selectedPlaylistId}`);
    state.playlistItems = Array.isArray(detail?.items) ? detail.items : [];
  }
  renderPlaylistEditor();
}

async function createPlaylist(event) {
  event.preventDefault();
  const payload = { station_id: Number(state.stationId), name: $('playlistName').value.trim(), description: $('playlistDescription').value.trim() };
  if (!payload.station_id || !payload.name) return setResult('playlistCatalogResult', 'Select a station and enter a playlist name.', 'error');
  setBusy(true, 'Creating playlist...', 'Saving and reading the playlist back');
  try {
    const created = await api('/api/playlists', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
    const playlistId = Number(created?.playlist_id || created?.id || 0);
    await loadPlaylists(playlistId);
    const stored = state.playlists.find((item) => Number(item.id) === playlistId);
    if (!stored || stored.name !== payload.name) throw new Error('Created playlist did not match read-back');
    const message = `Verified: playlist "${payload.name}" was created.`; setResult('playlistCatalogResult', message, 'success'); logActivity(message);
    $('playlistName').value = ''; $('playlistDescription').value = '';
  } catch (error) {
    const message = errorMessage(error); setResult('playlistCatalogResult', message, 'error'); logActivity(`Playlist create failed: ${message}`, 'error');
  } finally { setBusy(false); }
}

async function generatePlaylist(event) {
  event.preventDefault();
  const minRaw = $('playlistAutoBpmMin').value.trim(); const maxRaw = $('playlistAutoBpmMax').value.trim();
  const payload = {
    station_id: Number(state.stationId), name: $('playlistAutoName').value.trim(), description: '',
    artist: $('playlistAutoArtist').value.trim() || null, genre: $('playlistAutoGenre').value.trim() || null,
    track_type: $('playlistAutoTrackType').value, bpm_min: minRaw ? Number(minRaw) : null, bpm_max: maxRaw ? Number(maxRaw) : null,
    limit: Number($('playlistAutoLimit').value || 50), sort_by: $('playlistAutoSort').value,
  };
  if (!payload.station_id || !payload.name) return setResult('playlistCatalogResult', 'Select a station and enter an automatic-playlist name.', 'error');
  if (payload.bpm_min !== null && payload.bpm_max !== null && payload.bpm_max < payload.bpm_min) return setResult('playlistCatalogResult', 'Maximum BPM cannot be below minimum BPM.', 'error');
  setBusy(true, 'Generating playlist...', 'Selecting tracks and verifying the stored order');
  try {
    const created = await api('/api/playlists/auto/generate', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
    const playlistId = Number(created?.playlist_id || 0);
    await loadPlaylists(playlistId);
    if (!state.playlists.some((item) => Number(item.id) === playlistId) || state.playlistItems.length !== Number(created?.track_count || 0)) throw new Error('Generated playlist count did not match read-back');
    const message = `Verified: automatic playlist "${payload.name}" contains ${state.playlistItems.length} track(s).`; setResult('playlistCatalogResult', message, 'success'); logActivity(message);
  } catch (error) {
    const message = errorMessage(error); setResult('playlistCatalogResult', message, 'error'); logActivity(`Playlist generation failed: ${message}`, 'error');
  } finally { setBusy(false); }
}

async function addPlaylistItem(event) {
  event.preventDefault();
  const playlistId = Number(state.selectedPlaylistId); const trackId = Number($('playlistTrackId').value);
  if (!playlistId || !Number.isInteger(trackId) || trackId <= 0) return setResult('playlistEditorResult', 'Select a playlist and enter a positive track ID.', 'error');
  try {
    const created = await api(`/api/playlists/${playlistId}/items`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ track_id: trackId }) });
    await loadPlaylists(playlistId);
    if (!state.playlistItems.some((item) => Number(item.id) === Number(created?.item_id || created?.id))) throw new Error('Added playlist item was missing from read-back');
    const message = `Verified: track ${trackId} was added to the playlist.`; setResult('playlistEditorResult', message, 'success'); logActivity(message);
  } catch (error) { setResult('playlistEditorResult', errorMessage(error), 'error'); }
}

async function bulkAddPlaylistItems(event) {
  event.preventDefault();
  const playlistId = Number(state.selectedPlaylistId); let trackIds;
  try { trackIds = parseAdIdList($('playlistBulkTrackIds').value, 'Track IDs'); } catch (error) { return setResult('playlistEditorResult', errorMessage(error), 'error'); }
  if (!playlistId || !trackIds.length) return setResult('playlistEditorResult', 'Select a playlist and enter at least one track ID.', 'error');
  try {
    const created = await api(`/api/playlists/${playlistId}/bulk`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ track_ids: trackIds }) });
    const itemIds = (created?.item_ids || []).map(Number);
    await loadPlaylists(playlistId);
    if (itemIds.length !== trackIds.length || itemIds.some((id) => !state.playlistItems.some((item) => Number(item.id) === id))) throw new Error('Bulk-added playlist items did not match read-back');
    const message = `Verified: ${itemIds.length} track(s) were added to the playlist.`; setResult('playlistEditorResult', message, 'success'); logActivity(message);
  } catch (error) { setResult('playlistEditorResult', errorMessage(error), 'error'); }
}

async function movePlaylistItem(itemId, direction) {
  const playlistId = Number(state.selectedPlaylistId); const index = state.playlistItems.findIndex((item) => Number(item.id) === Number(itemId));
  const destination = direction === 'up' ? index - 1 : index + 1;
  if (!playlistId || index < 0 || destination < 0 || destination >= state.playlistItems.length) return;
  const expected = state.playlistItems.map((item) => Number(item.id));
  [expected[index], expected[destination]] = [expected[destination], expected[index]];
  try {
    await api(`/api/playlists/${playlistId}/reorder`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ item_ids: expected }) });
    await loadPlaylists(playlistId);
    if (JSON.stringify(state.playlistItems.map((item) => Number(item.id))) !== JSON.stringify(expected)) throw new Error('Playlist order did not match read-back');
    setResult('playlistEditorResult', 'Verified: playlist order was updated.', 'success');
  } catch (error) { setResult('playlistEditorResult', errorMessage(error), 'error'); }
}

async function removePlaylistItem(itemId) {
  const playlistId = Number(state.selectedPlaylistId); const id = Number(itemId); if (!playlistId || !id) return;
  try {
    await api(`/api/playlists/${playlistId}/items/${id}`, { method: 'DELETE' });
    await loadPlaylists(playlistId);
    if (state.playlistItems.some((item) => Number(item.id) === id)) throw new Error('Removed playlist item still appeared in read-back');
    setResult('playlistEditorResult', 'Verified: playlist item was removed.', 'success');
  } catch (error) { setResult('playlistEditorResult', errorMessage(error), 'error'); }
}

function disarmPlaylistDelete() {
  state.playlistDeleteArmedUntil = 0;
  if ($('deletePlaylistButton')) { $('deletePlaylistButton').classList.remove('armed'); $('deletePlaylistButton').textContent = 'Delete playlist'; }
}

async function deleteSelectedPlaylist() {
  const playlistId = Number(state.selectedPlaylistId); if (!playlistId) return;
  if (state.playlistDeleteArmedUntil <= Date.now()) {
    state.playlistDeleteArmedUntil = Date.now() + 20000; $('deletePlaylistButton').classList.add('armed'); $('deletePlaylistButton').textContent = 'Confirm delete playlist';
    setResult('playlistEditorResult', 'Click "Confirm delete playlist" again within 20 seconds.', 'error');
    window.setTimeout(() => { if (state.playlistDeleteArmedUntil <= Date.now()) disarmPlaylistDelete(); }, 20500); return;
  }
  disarmPlaylistDelete();
  try {
    await api(`/api/playlists/${playlistId}`, { method: 'DELETE' }); await loadPlaylists(0);
    if (state.playlists.some((item) => Number(item.id) === playlistId)) throw new Error('Deleted playlist still appeared in read-back');
    const message = 'Verified: selected playlist was deleted.'; setResult('playlistEditorResult', message, 'success'); logActivity(message);
  } catch (error) { setResult('playlistEditorResult', errorMessage(error), 'error'); }
}

function renderGuestRecordings() {
  $('guestRecordingCount').textContent = String(state.guestRecordings.length);
  $('guestRecordingList').innerHTML = state.guestRecordings.length ? state.guestRecordings.map((recording) => {
    const id = Number(recording.id);
    const armed = (state.guestRecordingDeleteArmed[id] || 0) > Date.now();
    const canDownload = Boolean(String(recording.file_path || '').trim()) && ['completed', 'stopped', 'interrupted'].includes(String(recording.status || '').toLowerCase());
    return `<div class="record-row"><div class="record-copy"><b>Guest recording ${id}</b><span>${escapeHtml(String(recording.status || 'unknown').replaceAll('_', ' '))}</span><small>Started ${escapeHtml(recording.started_at || recording.created_at || 'not started')} / stopped ${escapeHtml(recording.stopped_at || 'not stopped')}</small></div><div class="row-actions">${canDownload ? `<button class="icon-button" type="button" data-guest-recording-download="${id}">Download</button>` : ''}<button class="icon-button remove${armed ? ' armed' : ''}" type="button" data-guest-recording-delete="${id}">${armed ? 'Confirm delete' : 'Delete'}</button></div></div>`;
  }).join('') : '<div class="empty-state">No guest recordings exist for this station.</div>';
}

async function loadGuestRecordings() {
  if (!state.stationId) return;
  const payload = await api(`/api/guest-recordings?station_id=${Number(state.stationId)}`);
  state.guestRecordings = payload?.recordings || [];
  renderGuestRecordings();
}

async function downloadGuestRecording(recordingId, retry = true) {
  const id = Number(recordingId);
  const headers = new Headers();
  const token = localStorage.getItem(AUTH_KEYS.access);
  if (token) headers.set('Authorization', `Bearer ${token}`);
  const response = await rawFetch(`/api/guest-recordings/${id}/download`, { headers }, 120000);
  if (response.status === 401 && retry && await refreshSession()) return downloadGuestRecording(id, false);
  if (!response.ok) throw parseResponseError(await response.text(), response.status, response.headers.get('X-Request-ID') || '');
  const objectUrl = URL.createObjectURL(await response.blob());
  try {
    const link = document.createElement('a');
    link.href = objectUrl;
    link.download = `guest-recording-${id}.flac`;
    link.click();
    setResult('guestRecordingLibraryResult', `Downloaded guest recording ${id}.`, 'success');
  } finally {
    window.setTimeout(() => URL.revokeObjectURL(objectUrl), 1000);
  }
}

function clearGuestRecordingDeleteArms() {
  state.guestRecordingDeleteArmed = {};
  if ($('guestRecordingList')) renderGuestRecordings();
}

async function deleteGuestRecording(recordingId) {
  const id = Number(recordingId);
  if (!state.guestRecordings.some((item) => Number(item.id) === id)) return;
  const now = Date.now();
  if ((state.guestRecordingDeleteArmed[id] || 0) <= now) {
    state.guestRecordingDeleteArmed = { [id]: now + 20000 };
    renderGuestRecordings();
    setResult('guestRecordingLibraryResult', `Click "Confirm delete" for recording ${id} again within 20 seconds.`, 'error');
    window.setTimeout(() => { if ((state.guestRecordingDeleteArmed[id] || 0) <= Date.now()) clearGuestRecordingDeleteArms(); }, 20500);
    return;
  }
  clearGuestRecordingDeleteArms();
  setBusy(true, 'Deleting guest recording...', `Removing recording ${id} through the protected API`);
  try {
    await api(`/api/guest-recordings/${id}`, { method: 'DELETE' });
    await loadGuestRecordings();
    if (state.guestRecordings.some((item) => Number(item.id) === id)) throw new Error('Deleted guest recording still appeared in read-back');
    const message = `Verified: guest recording ${id} and its managed files were deleted.`;
    setResult('guestRecordingLibraryResult', message, 'success'); logActivity(message);
  } catch (error) {
    const message = errorMessage(error); setResult('guestRecordingLibraryResult', message, 'error'); logActivity(`Guest recording delete failed: ${message}`, 'error');
  } finally { setBusy(false); }
}

function localDateTimeInputValue(date) {
  const value = new Date(date);
  value.setMinutes(value.getMinutes() - value.getTimezoneOffset());
  return value.toISOString().slice(0, 16);
}

function initializeAdDefaults() {
  if (!$('adItemDueAt').value) $('adItemDueAt').value = localDateTimeInputValue(Date.now() + 5 * 60 * 1000);
}

function parseAdIdList(value, label) {
  const values = String(value || '').split(',').map((item) => item.trim()).filter(Boolean);
  const parsed = values.map((item) => Number(item));
  if (parsed.some((item) => !Number.isInteger(item) || item <= 0)) throw new Error(`${label} must contain positive numeric IDs separated by commas`);
  return [...new Set(parsed)];
}

function parseAdSlots() {
  const raw = $('adBreakSlots').value.trim();
  if (!raw) return [];
  let slots;
  try { slots = JSON.parse(raw); } catch (_) { throw new Error('Break slots must be a valid JSON array'); }
  if (!Array.isArray(slots) || slots.some((slot) => !slot || typeof slot !== 'object' || Array.isArray(slot) || !String(slot.slot_time || '').trim())) {
    throw new Error('Every break slot must be an object with slot_time');
  }
  return slots;
}

function renderAdItems() {
  $('adItemCount').textContent = String(state.adItems.length);
  $('adItemList').innerHTML = state.adItems.length ? state.adItems.map((item) => `
    <div class="record-row"><div class="record-copy"><b>${escapeHtml(item.title || `Track ${item.track_id}`)}</b><span>${escapeHtml(item.artist || 'Unknown artist')}</span><small>Track ${Number(item.track_id)} / priority ${Number(item.priority || 0)}</small></div><div class="record-meta"><span>${escapeHtml(item.status || 'pending')}</span><small>${escapeHtml(item.due_at || '')}</small></div></div>`).join('') : '<div class="empty-state">No advertising items are queued for this station.</div>';
}

function renderAdRuntime() {
  const runtime = state.adRuntime || {};
  const due = Array.isArray(runtime.due_slots) ? runtime.due_slots : [];
  const next = Array.isArray(runtime.next_slots) ? runtime.next_slots : [];
  $('adRuntimeState').textContent = `${Number(runtime.break_set_count || 0)} break set / ${Number(runtime.campaign_count || 0)} campaign`;
  const rows = [
    ...due.map((item) => ({ label: 'Due now', item })),
    ...next.map((item) => ({ label: 'Upcoming', item })),
  ];
  $('adRuntimeList').innerHTML = rows.length ? rows.map(({ label, item }) => `
    <div class="record-row"><div class="record-copy"><b>${escapeHtml(item.name || item.title || label)}</b><span>${escapeHtml(item.slot_time || item.due_at || JSON.stringify(item))}</span></div><div class="record-meta"><span>${label}</span></div></div>`).join('') : '<div class="empty-state">No due or upcoming advertising breaks.</div>';
}

function renderAdBreakSetEditor() {
  $('adBreakSetCount').textContent = String(state.adBreakSets.length);
  $('adBreakSetSelect').innerHTML = '<option value="0">Create a new break set</option>' + state.adBreakSets.map((item) => `<option value="${Number(item.id)}">${escapeHtml(item.name)}</option>`).join('');
  $('adBreakSetSelect').value = String(state.selectedAdBreakSetId || 0);
  const item = state.adBreakSets.find((row) => Number(row.id) === Number(state.selectedAdBreakSetId));
  $('adBreakSetName').value = item?.name || '';
  $('adBreakSetDescription').value = item?.description || '';
  $('adBreakIntroTrackId').value = item?.intro_jingle_track_id || '';
  $('adBreakOutroTrackId').value = item?.outro_jingle_track_id || '';
  $('adBreakSlots').value = item ? JSON.stringify(item.slots || [], null, 2) : '';
  $('adBreakSetActive').checked = item ? Boolean(item.is_active) : true;
  $('saveAdBreakSetButton').textContent = item ? 'Save and verify break set' : 'Create and verify break set';
  $('deleteAdBreakSetButton').hidden = !item;
}

function renderAdCampaignEditor() {
  $('adCampaignCount').textContent = String(state.adCampaigns.length);
  $('adCampaignSelect').innerHTML = '<option value="0">Create a new campaign</option>' + state.adCampaigns.map((item) => `<option value="${Number(item.id)}">${escapeHtml(item.name)}</option>`).join('');
  $('adCampaignSelect').value = String(state.selectedAdCampaignId || 0);
  const item = state.adCampaigns.find((row) => Number(row.id) === Number(state.selectedAdCampaignId));
  $('adCampaignName').value = item?.name || '';
  $('adCampaignStartDate').value = item?.start_date || '';
  $('adCampaignEndDate').value = item?.end_date || '';
  $('adCampaignDayInterval').value = item?.day_interval || 1;
  $('adCampaignDailyLimit').value = item?.daily_repeat_limit ?? 0;
  $('adCampaignPriority').value = item?.priority ?? 0;
  $('adCampaignSlotIds').value = (item?.slot_ids || []).join(', ');
  $('adCampaignTrackIds').value = (item?.track_ids || []).join(', ');
  $('adCampaignNotes').value = item?.notes || '';
  $('adCampaignActive').checked = item ? Boolean(item.is_active) : true;
  $('saveAdCampaignButton').textContent = item ? 'Save and verify campaign' : 'Create and verify campaign';
  $('deleteAdCampaignButton').hidden = !item;
}

async function loadAdvertising(preferredBreakSetId = state.selectedAdBreakSetId, preferredCampaignId = state.selectedAdCampaignId) {
  if (!state.stationId) return;
  const stationId = Number(state.stationId);
  const [items, runtime, breakSets, campaigns] = await Promise.all([
    api(`/api/ads/items?station_id=${stationId}&limit=50`),
    api(`/api/ads/runtime?station_id=${stationId}`),
    api(`/api/ad-break-sets?station_id=${stationId}`),
    api(`/api/ad-campaigns?station_id=${stationId}`),
  ]);
  state.adItems = items?.items || [];
  state.adRuntime = runtime || {};
  state.adBreakSets = breakSets?.break_sets || [];
  state.adCampaigns = campaigns?.campaigns || [];
  state.selectedAdBreakSetId = state.adBreakSets.some((item) => Number(item.id) === Number(preferredBreakSetId)) ? Number(preferredBreakSetId) : 0;
  state.selectedAdCampaignId = state.adCampaigns.some((item) => Number(item.id) === Number(preferredCampaignId)) ? Number(preferredCampaignId) : 0;
  renderAdItems();
  renderAdRuntime();
  renderAdBreakSetEditor();
  renderAdCampaignEditor();
  initializeAdDefaults();
}

async function enqueueAdItem(event) {
  event.preventDefault();
  const payload = {
    station_id: Number(state.stationId),
    track_id: Number($('adItemTrackId').value),
    due_at: $('adItemDueAt').value,
    priority: Number($('adItemPriority').value || 0),
  };
  if (!payload.station_id || !Number.isInteger(payload.track_id) || payload.track_id <= 0 || !payload.due_at) return setResult('adItemResult', 'Select a station, track, and due time.', 'error');
  setBusy(true, 'Queueing advertising item...', 'Saving and reading the item back');
  try {
    const created = await api('/api/ads/items', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
    await loadAdvertising();
    if (!state.adItems.some((item) => Number(item.id) === Number(created?.item_id))) throw new Error('Ad item was created but missing from read-back');
    const message = `Verified: ad item ${Number(created.item_id)} is in the station queue.`;
    setResult('adItemResult', message, 'success'); logActivity(message);
  } catch (error) {
    const message = errorMessage(error); setResult('adItemResult', message, 'error'); logActivity(`Ad item enqueue failed: ${message}`, 'error');
  } finally { setBusy(false); }
}

async function saveAdBreakSet(event) {
  event.preventDefault();
  let slots;
  try { slots = parseAdSlots(); } catch (error) { return setResult('adBreakSetResult', errorMessage(error), 'error'); }
  const currentId = Number(state.selectedAdBreakSetId || 0);
  const payload = {
    station_id: Number(state.stationId),
    name: $('adBreakSetName').value.trim(),
    description: $('adBreakSetDescription').value.trim(),
    is_active: $('adBreakSetActive').checked,
    intro_jingle_track_id: Number($('adBreakIntroTrackId').value || 0) || null,
    outro_jingle_track_id: Number($('adBreakOutroTrackId').value || 0) || null,
    slots,
  };
  if (!payload.station_id || !payload.name) return setResult('adBreakSetResult', 'Select a station and enter a break-set name.', 'error');
  setBusy(true, 'Saving ad break set...', 'Writing and reading the configuration back');
  try {
    const saved = await api(currentId ? `/api/ad-break-sets/${currentId}` : '/api/ad-break-sets', { method: currentId ? 'PUT' : 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
    const savedId = currentId || Number(saved?.break_set_id || saved?.id || 0);
    await loadAdvertising(savedId, state.selectedAdCampaignId);
    const stored = state.adBreakSets.find((item) => Number(item.id) === savedId);
    const expectedTimes = slots.map((item) => String(item.slot_time || '').slice(0, 5)).filter(Boolean);
    const storedTimes = (stored?.slots || []).map((item) => String(item.slot_time || '').slice(0, 5));
    if (!stored || stored.name !== payload.name || Boolean(stored.is_active) !== payload.is_active || JSON.stringify(storedTimes) !== JSON.stringify(expectedTimes)) throw new Error('Break-set read-back did not match the saved values');
    const message = `Verified: break set "${payload.name}" matches stored configuration.`;
    setResult('adBreakSetResult', message, 'success'); logActivity(message);
  } catch (error) {
    const message = errorMessage(error); setResult('adBreakSetResult', message, 'error'); logActivity(`Break-set save failed: ${message}`, 'error');
  } finally { setBusy(false); }
}

async function saveAdCampaign(event) {
  event.preventDefault();
  let slotIds; let trackIds;
  try {
    slotIds = parseAdIdList($('adCampaignSlotIds').value, 'Break-set IDs');
    trackIds = parseAdIdList($('adCampaignTrackIds').value, 'Track IDs');
  } catch (error) { return setResult('adCampaignResult', errorMessage(error), 'error'); }
  const currentId = Number(state.selectedAdCampaignId || 0);
  const payload = {
    station_id: Number(state.stationId),
    name: $('adCampaignName').value.trim(),
    is_active: $('adCampaignActive').checked,
    start_date: $('adCampaignStartDate').value,
    end_date: $('adCampaignEndDate').value,
    day_interval: Number($('adCampaignDayInterval').value || 1),
    daily_repeat_limit: Number($('adCampaignDailyLimit').value || 0),
    priority: Number($('adCampaignPriority').value || 0),
    notes: $('adCampaignNotes').value.trim(),
    slot_ids: slotIds,
    track_ids: trackIds,
  };
  if (!payload.station_id || !payload.name) return setResult('adCampaignResult', 'Select a station and enter a campaign name.', 'error');
  if (payload.start_date && payload.end_date && payload.end_date < payload.start_date) return setResult('adCampaignResult', 'Campaign end date cannot be before its start date.', 'error');
  setBusy(true, 'Saving ad campaign...', 'Writing and reading the campaign back');
  try {
    const saved = await api(currentId ? `/api/ad-campaigns/${currentId}` : '/api/ad-campaigns', { method: currentId ? 'PUT' : 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
    const savedId = currentId || Number(saved?.campaign_id || saved?.id || 0);
    await loadAdvertising(state.selectedAdBreakSetId, savedId);
    const stored = state.adCampaigns.find((item) => Number(item.id) === savedId);
    if (!stored || stored.name !== payload.name || Boolean(stored.is_active) !== payload.is_active || JSON.stringify(stored.slot_ids || []) !== JSON.stringify(slotIds) || JSON.stringify(stored.track_ids || []) !== JSON.stringify(trackIds)) throw new Error('Campaign read-back did not match the saved values');
    const message = `Verified: campaign "${payload.name}" matches stored configuration.`;
    setResult('adCampaignResult', message, 'success'); logActivity(message);
  } catch (error) {
    const message = errorMessage(error); setResult('adCampaignResult', message, 'error'); logActivity(`Campaign save failed: ${message}`, 'error');
  } finally { setBusy(false); }
}

function clearAdDeleteArms() {
  state.adDeleteArmed = {};
  [['deleteAdBreakSetButton', 'Delete break set'], ['deleteAdCampaignButton', 'Delete campaign']].forEach(([id, label]) => {
    const button = $(id); if (!button) return; button.classList.remove('armed'); button.textContent = label;
  });
}

async function deleteAdvertisingEntity(kind) {
  const isBreakSet = kind === 'break-set';
  const id = Number(isBreakSet ? state.selectedAdBreakSetId : state.selectedAdCampaignId);
  const button = $(isBreakSet ? 'deleteAdBreakSetButton' : 'deleteAdCampaignButton');
  const resultId = isBreakSet ? 'adBreakSetResult' : 'adCampaignResult';
  if (!id) return;
  const now = Date.now();
  if ((state.adDeleteArmed[kind] || 0) <= now) {
    clearAdDeleteArms(); state.adDeleteArmed[kind] = now + 20000; button.classList.add('armed'); button.textContent = `Confirm ${button.textContent}`;
    setResult(resultId, `Click "${button.textContent}" again within 20 seconds.`, 'error');
    window.setTimeout(() => { if ((state.adDeleteArmed[kind] || 0) <= Date.now()) clearAdDeleteArms(); }, 20500);
    return;
  }
  clearAdDeleteArms();
  setBusy(true, `Deleting ${kind}...`, 'Removing only the selected station record');
  try {
    const endpoint = isBreakSet ? `/api/ad-break-sets/${id}` : `/api/ad-campaigns/${id}`;
    await api(`${endpoint}?station_id=${Number(state.stationId)}`, { method: 'DELETE' });
    await loadAdvertising(isBreakSet ? 0 : state.selectedAdBreakSetId, isBreakSet ? state.selectedAdCampaignId : 0);
    const remains = (isBreakSet ? state.adBreakSets : state.adCampaigns).some((item) => Number(item.id) === id);
    if (remains) throw new Error('Deleted advertising record still appeared in read-back');
    const message = `Verified: selected ${kind} was deleted.`; setResult(resultId, message, 'success'); logActivity(message);
  } catch (error) {
    const message = errorMessage(error); setResult(resultId, message, 'error'); logActivity(`${kind} delete failed: ${message}`, 'error');
  } finally { setBusy(false); }
}

function renderHlsSettings() {
  const hls = state.hlsSettings || {};
  const running = hls.status === 'running' && hls.playlist_active === true;
  const available = hls.runtime_available === true;
  $('hlsEnabled').checked = Boolean(hls.enabled);
  $('hlsEnabled').disabled = true;
  $('hlsCodecProfile').value = 'he_aac_v1_96_192';
  $('hlsRuntimeStatus').value = available
    ? (running ? 'Çalışıyor · playlist aktif' : 'Hazır · kapalı')
    : 'libfdk_aac bulunamadı';
  const outputRoot = $('hlsOutputRoot');
  if (outputRoot) outputRoot.value = hls.output_root || '—';
  $('hlsSettingsState').textContent = running
    ? 'On · HE-AAC v1 96/192'
    : (hls.status === 'error' ? 'Hata · başlatılamadı' : 'Off · hazır');
  $('startHlsButton').disabled = running || !available;
  $('stopHlsButton').disabled = !running;
  const homeState = $('hlsHomeState');
  if (homeState) homeState.textContent = running ? 'On · canlı' : (available ? 'Off · hazır' : 'Encoder yok');
  const homePlaylist = $('hlsHomePlaylist');
  if (homePlaylist) homePlaylist.textContent = hls.playlist_active ? 'Aktif' : 'Yok';
  const homeEncoder = $('hlsHomeEncoder');
  if (homeEncoder) homeEncoder.textContent = hls.encoder || 'libfdk_aac';
  const homeStart = $('startHlsHomeButton');
  if (homeStart) homeStart.disabled = running || !available;
}

async function loadHlsSettings() {
  state.hlsSettings = await api('/api/settings/hls');
  renderHlsSettings();
  return state.hlsSettings;
}

async function startHls() {
  setBusy(true, 'HLS başlatılıyor...', 'Altı Icecast mount’unda ses byte’ları ve HE-AAC playlistleri doğrulanıyor');
  setResult('hlsSettingsResult');
  setResult('hlsHomeResult');
  try {
    const response = await api('/api/settings/hls/start', { method: 'POST' });
    const stored = await loadHlsSettings();
    if (!response?.ok || stored.status !== 'running' || !stored.playlist_active || !stored.enabled) {
      throw new Error('HLS başlatıldı bildirimi playlist read-back ile doğrulanamadı');
    }
    const message = 'Doğrulandı: HLS canlı; altı radyo için HE-AAC v1 Low 96 / High 192 playlistleri aktif.';
    setResult('hlsSettingsResult', message, 'success');
    setResult('hlsHomeResult', message, 'success');
    logActivity(message);
  } catch (error) {
    const message = errorMessage(error);
    setResult('hlsSettingsResult', message, 'error');
    setResult('hlsHomeResult', message, 'error');
    logActivity(`HLS policy save failed: ${message}`, 'error');
  } finally {
    setBusy(false);
  }
}

async function stopHls() {
  setBusy(true, 'HLS durduruluyor...', 'Yalnızca HLS writer süreçleri kapatılıyor; Icecast normal yayınları korunuyor');
  setResult('hlsSettingsResult');
  setResult('hlsHomeResult');
  try {
    await api('/api/settings/hls/stop', { method: 'POST' });
    const stored = await loadHlsSettings();
    if (stored.enabled || stored.status === 'running' || stored.playlist_active) {
      throw new Error('HLS durdurma read-back ile doğrulanamadı');
    }
    const message = 'Doğrulandı: HLS kapalı; Icecast/TinyIce normal mount’ları etkilenmedi.';
    setResult('hlsSettingsResult', message, 'success');
    setResult('hlsHomeResult', message, 'success');
    logActivity(message);
  } catch (error) {
    const message = errorMessage(error);
    setResult('hlsSettingsResult', message, 'error');
    setResult('hlsHomeResult', message, 'error');
    logActivity(`HLS stop failed: ${message}`, 'error');
  } finally {
    setBusy(false);
  }
}

async function refreshHlsStatus() {
  try {
    await loadHlsSettings();
    const message = state.hlsSettings?.status === 'running' ? 'HLS canlı durumu yenilendi.' : 'HLS hazır durumu yenilendi.';
    setResult('hlsSettingsResult', message, 'success');
    setResult('hlsHomeResult', message, 'success');
  } catch (error) {
    const message = errorMessage(error);
    setResult('hlsSettingsResult', message, 'error');
    setResult('hlsHomeResult', message, 'error');
  }
}

const STREAMING_FEATURE_FIELDS = Object.freeze({
  stream_public_base_url: 'streamPublicBaseUrl',
  radio_website_url: 'radioWebsiteUrl',
  rocket_admin_user: 'rocketAdminUser',
  rocket_status_page_enabled: 'rocketStatusPageEnabled',
  rocket_fallbacks_enabled: 'rocketFallbacksEnabled',
  rocket_listener_auth_enabled: 'rocketListenerAuthEnabled',
  rocket_ad_insertion_enabled: 'rocketAdInsertionEnabled',
  rocket_access_log_enabled: 'rocketAccessLogEnabled',
  rocket_playlist_log_enabled: 'rocketPlaylistLogEnabled',
});

function selectedStreamingMount() {
  const entered = $('streamingMount').value.trim();
  const configured = String(state.stationOutput?.icecast_mount || '').trim();
  const mount = entered || configured;
  if (!mount) return '';
  return mount.startsWith('/') ? mount : `/${mount}`;
}

function renderStreamingFeatures() {
  const features = state.streamingFeatures || {};
  Object.entries(STREAMING_FEATURE_FIELDS).forEach(([key, id]) => {
    if (typeof features[key] === 'boolean') setCleanChecked(id, features[key]);
    else setCleanValue(id, features[key] ?? '');
  });
  const secrets = [
    features.rocket_admin_password_set ? 'administrator secret configured' : 'administrator secret missing',
    features.rocket_health_password_set ? 'health secret configured' : 'health secret missing',
  ];
  $('streamingSecretState').textContent = secrets.join(' / ');
  if ($('streamingMount').dataset.dirty !== '1') $('streamingMount').value = selectedStreamingMount();
  $('streamingMountState').textContent = selectedStreamingMount() || 'No mount selected';
}

async function loadStreamingFeatures() {
  const payload = await api('/api/streaming/features');
  state.streamingFeatures = payload?.system || {};
  renderStreamingFeatures();
  return payload;
}

function renderQualityOutputs() {
  const payload = state.qualityOutputs || {};
  const channels = Array.isArray(payload.channels) ? payload.channels : [];
  $('qualityOutputsState').textContent = channels.length
    ? `${Number(payload.local_mount_count || 14)} local / ${Number(payload.system_mount_count || 16)} system mounts`
    : 'Not configured';
  if (!channels.length) {
    $('qualityOutputsList').innerHTML = '<div class="empty-state">No quality channels were returned.</div>';
    return;
  }
  $('qualityOutputsList').innerHTML = channels.map((channel) => {
    const mapped = channel.external || channel.station_found !== false;
    const credentials = channel.credential_status === 'managed_by_external_supervisor'
      ? 'credentials inherited inside AI supervisor'
      : (channel.credential_configured ? 'protected credentials ready' : 'source credentials need attention');
    const variants = Array.isArray(channel.variants) ? channel.variants : [];
    const primary = channel.primary || {};
    return `<section class="quality-output-card" data-quality-channel="${escapeHtml(channel.channel_id)}">
      <div class="quality-output-card-head"><div><b>${escapeHtml(channel.label)}</b><span>${escapeHtml(channel.base_mount)} · ${channel.external ? 'AI supervisor' : `station ${channel.station_id ?? 'unmapped'}`}</span></div><span>${mapped ? escapeHtml(credentials) : 'station mapping missing'}</span></div>
      <div class="quality-variant-row"><div class="quality-variant-copy"><b>Primary · ${escapeHtml(primary.codec || 'Other')}${Number(primary.bitrate_kbps) > 0 ? ` ${Number(primary.bitrate_kbps)} kbps` : ''}</b><code>${escapeHtml(primary.mount || channel.base_mount)} · managed in Stations</code></div><div class="quality-variant-controls"><span>${primary.enabled ? 'On' : 'Off'}</span></div></div>
      ${variants.map((variant) => `<div class="quality-variant-row" data-quality-variant="${escapeHtml(variant.quality)}">
        <div class="quality-variant-copy"><b>${escapeHtml(variant.label)} · ${escapeHtml(variant.codec)}${Number(variant.bitrate_kbps) > 0 ? ` ${Number(variant.bitrate_kbps)} kbps` : ''}</b><code>${escapeHtml(variant.mount)}</code></div>
        <div class="quality-variant-controls"><label class="check-row"><input data-quality-enabled type="checkbox" ${variant.enabled ? 'checked' : ''}> On</label><label class="check-row"><input data-quality-public type="checkbox" ${variant.icecast_public ? 'checked' : ''}> Public</label></div>
      </div>`).join('')}
    </section>`;
  }).join('');
}

async function loadQualityOutputs() {
  state.qualityOutputs = await api('/api/streaming/quality-outputs');
  if ($('qualityOriginSourceCapacity')) $('qualityOriginSourceCapacity').value = Number(state.qualityOutputs?.origin_source_capacity || 20);
  renderQualityOutputs();
  return state.qualityOutputs;
}

function qualityOutputsPayload() {
  return {
    origin_source_capacity: Number($('qualityOriginSourceCapacity')?.value || 0),
    channels: [...document.querySelectorAll('[data-quality-channel]')].map((channel) => {
      const variants = {};
      channel.querySelectorAll('[data-quality-variant]').forEach((row) => {
        variants[row.dataset.qualityVariant] = {
          enabled: Boolean(row.querySelector('[data-quality-enabled]')?.checked),
          icecast_public: Boolean(row.querySelector('[data-quality-public]')?.checked),
        };
      });
      return { channel_id: channel.dataset.qualityChannel, variants };
    }),
  };
}

function qualityOutputsMatch(expected, stored) {
  const storedChannels = new Map((stored?.channels || []).map((item) => [item.channel_id, item]));
  return Number(stored?.origin_source_capacity || 0) === Number(expected.origin_source_capacity || 0) && expected.channels.every((channel) => {
    const saved = storedChannels.get(channel.channel_id);
    if (!saved) return false;
    const savedVariants = new Map((saved.variants || []).map((item) => [item.quality, item]));
    return Object.entries(channel.variants).every(([quality, wanted]) => {
      const actual = savedVariants.get(quality);
      return actual
        && Boolean(actual.enabled) === Boolean(wanted.enabled)
        && Boolean(actual.icecast_public) === Boolean(wanted.icecast_public);
    });
  });
}

async function saveQualityOutputs(event) {
  event.preventDefault();
  const expected = qualityOutputsPayload();
  if (expected.channels.length !== 6) {
    setResult('qualityOutputsResult', 'All six music quality channels must be loaded before saving.', 'error');
    return;
  }
    setBusy(true, 'Saving quality outputs...', 'Persisting the 16-mount plan and reading every setting back');
  setResult('qualityOutputsResult');
  try {
    const saved = await api('/api/streaming/quality-outputs', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(expected),
    });
    if (saved?.external_bridge?.ok !== true) {
      throw new Error('Settings were stored, but the durable legacy-only AI bridge could not be verified');
    }
    const stored = await loadQualityOutputs();
    if (!qualityOutputsMatch(expected, stored)) {
      throw new Error('Quality settings were saved but the read-back did not match');
    }
    const message = 'Verified: 14 local music mounts plus 2 external AI mounts are persisted as the approved 16-mount plan; protected credentials were not copied.';
    setResult('qualityOutputsResult', message, 'success');
    logActivity(message);
  } catch (error) {
    const message = errorMessage(error);
    setResult('qualityOutputsResult', message, 'error');
    logActivity(`Quality output save failed: ${message}`, 'error');
  } finally {
    setBusy(false);
  }
}

function renderQualityOutputDiagnostics(payload) {
  const runtime = Array.isArray(payload?.runtime) ? payload.runtime : [];
  const runtimeChecked = runtime.filter((item) => item.runtime_checked);
  const unhealthy = runtimeChecked.reduce((total, item) => total + (item.unhealthy_branches?.length || 0), 0);
  const capacity = payload?.origin_capacity || {};
  const observedHealthy = Number(capacity.observed_healthy_local_mounts || 0);
  const observedEnabled = Number(capacity.observed_enabled_local_mounts || payload?.enabled_local_mount_count || 0);
  const declaredCapacity = Number(capacity.configured_source_slots || 0);
  const encoder = payload?.fdk_aac_encoder || payload?.he_aac_encoder || {};
  const issues = Array.isArray(payload?.configuration_issues) ? payload.configuration_issues : [];
  const bridge = payload?.external_bridge || {};
  $('qualityOutputsDiagnostics').innerHTML = `<div class="record-row"><div><b>Deterministic mount plan</b><span>${Number(payload?.local_mount_count || 14)} local · ${Number(payload?.system_mount_count || 16)} system · ${Number(payload?.enabled_local_mount_count || 0)} local enabled</span></div><span>${issues.length ? `${issues.length} issue(s)` : 'verified'}</span></div>
    <div class="record-row"><div><b>AI streams</b><span>English and French stay on their existing single mounts</span></div><span>${bridge.ok ? 'legacy-only verified' : escapeHtml(bridge.error_code || 'not ready')}</span></div>
    <div class="record-row"><div><b>AAC encoder</b><span>FFmpeg libfdk_aac: AAC-LC 192 Normal and HE-AAC v2 64 Low; FLAC remains lossless for Classical and Cazz</span></div><span>${encoder.available ? 'verified' : escapeHtml(encoder.error_code || 'not ready')}</span></div>
    <div class="record-row"><div><b>Music runtime branches</b><span>${runtimeChecked.length} station runtime(s) checked</span></div><span>${unhealthy ? `${unhealthy} not healthy` : (runtimeChecked.length ? 'healthy' : 'not running')}</span></div>
    <div class="record-row"><div><b>Origin source delivery</b><span>${observedHealthy}/${observedEnabled} enabled local mounts delivering · ${declaredCapacity || 'no'} slots declared</span></div><span>${capacity.verified ? 'verified by audio delivery' : escapeHtml(capacity.warning || 'not verified')}</span></div>`;
  const encoderReady = encoder.available || !Number(encoder.required_by_enabled_mounts || 0);
  $('qualityOutputsState').textContent = payload?.ok && capacity.verified && encoderReady ? 'Diagnostics ready' : 'Attention required';
}

async function diagnoseQualityOutputs() {
  setBusy(true, 'Checking quality outputs...', 'Verifying runtime branches, durable bridge, and observed origin delivery');
  try {
    const payload = await api('/api/streaming/quality-outputs/diagnostics');
    renderQualityOutputDiagnostics(payload);
    const issueCount = Array.isArray(payload.configuration_issues) ? payload.configuration_issues.length : 0;
    const capacityReady = payload?.origin_capacity?.verified === true;
    const capacityWarning = capacityReady ? '' : ` ${payload?.origin_capacity?.warning || 'Origin delivery still requires verification.'}`;
    const diagnosticsReady = issueCount === 0 && capacityReady;
    setResult(
      'qualityOutputsResult',
      diagnosticsReady
        ? 'Quality configuration, AI bridge, and origin delivery verified.'
        : `${issueCount ? `${issueCount} configuration issue(s) require attention.` : 'Configuration is valid, but delivery is incomplete.'}${capacityWarning}`,
      diagnosticsReady ? 'success' : 'error',
    );
    return payload;
  } catch (error) {
    setResult('qualityOutputsResult', errorMessage(error), 'error');
    throw error;
  } finally {
    setBusy(false);
  }
}

async function applyQualityOutputsNow() {
  setBusy(true, 'Applying quality outputs...', 'Refreshing six music runtimes; English/French legacy streams stay untouched');
  try {
    const payload = await api('/api/streaming/quality-outputs/apply', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ restart_ai_supervisor: false }),
    });
    if (!payload?.ok) throw new Error('One or more quality output owners could not apply the saved settings');
    setResult('qualityOutputsResult', 'Saved AAC/FLAC outputs applied without sourcing the external AI streams or copying credentials.', 'success');
    logActivity('Quality outputs applied and verified.');
    return payload;
  } catch (error) {
    const message = errorMessage(error);
    setResult('qualityOutputsResult', message, 'error');
    logActivity(`Quality output apply failed: ${message}`, 'error');
    throw error;
  } finally {
    setBusy(false);
  }
}

function confirmQualityOutputApply() {
  const button = $('applyQualityOutputsButton');
  const now = Date.now();
  if (!state.qualityApplyArmedUntil || state.qualityApplyArmedUntil <= now) {
    state.qualityApplyArmedUntil = now + 20000;
    button.classList.add('armed');
    button.textContent = 'Confirm quality apply';
    setResult('qualityOutputsResult', 'Click “Confirm quality apply” again within 20 seconds. Legacy and AI streams are not restarted.', 'error');
    window.setTimeout(() => {
      if ((state.qualityApplyArmedUntil || 0) <= Date.now()) {
        button.classList.remove('armed');
        button.textContent = 'Apply saved outputs now';
      }
    }, 20500);
    return;
  }
  state.qualityApplyArmedUntil = 0;
  button.classList.remove('armed');
  button.textContent = 'Apply saved outputs now';
  applyQualityOutputsNow().then(() => diagnoseQualityOutputs()).catch(() => {});
}

function prepareApproved16MountPlan() {
  if ($('qualityOriginSourceCapacity')) $('qualityOriginSourceCapacity').value = '20';
  document.querySelectorAll('[data-quality-enabled], [data-quality-public]').forEach((node) => { node.checked = true; });
  setResult('qualityOutputsResult', 'Approved plan selected: all 8 quality variants are On and public, with 20 origin source slots recorded. Save and verify, then apply.', 'success');
}

function streamingFeaturePayload() {
  return {
    stream_public_base_url: $('streamPublicBaseUrl').value.trim(),
    radio_website_url: $('radioWebsiteUrl').value.trim(),
    rocket_admin_user: $('rocketAdminUser').value.trim() || 'admin',
    rocket_admin_password: $('rocketAdminPassword').value,
    rocket_health_password: $('rocketHealthPassword').value,
    rocket_status_page_enabled: $('rocketStatusPageEnabled').checked,
    rocket_fallbacks_enabled: $('rocketFallbacksEnabled').checked,
    rocket_listener_auth_enabled: $('rocketListenerAuthEnabled').checked,
    rocket_ad_insertion_enabled: $('rocketAdInsertionEnabled').checked,
    rocket_access_log_enabled: $('rocketAccessLogEnabled').checked,
    rocket_playlist_log_enabled: $('rocketPlaylistLogEnabled').checked,
  };
}

async function saveStreamingFeatures(event) {
  event.preventDefault();
  const expected = streamingFeaturePayload();
  setBusy(true, 'Saving streaming policy...', 'Writing settings and reading them back');
  setResult('streamingFeaturesResult');
  try {
    await api('/api/streaming/features', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(expected),
    });
    const stored = await loadStreamingFeatures();
    const system = stored?.system || {};
    const valueKeys = ['stream_public_base_url', 'radio_website_url', 'rocket_admin_user'];
    const boolKeys = Object.keys(STREAMING_FEATURE_FIELDS).filter((key) => key.endsWith('_enabled'));
    const valuesMatch = valueKeys.every((key) => String(system[key] ?? '') === String(expected[key] ?? ''));
    const booleansMatch = boolKeys.every((key) => Boolean(system[key]) === Boolean(expected[key]));
    const secretsMatch = (!expected.rocket_admin_password || system.rocket_admin_password_set)
      && (!expected.rocket_health_password || system.rocket_health_password_set);
    if (!valuesMatch || !booleansMatch || !secretsMatch) throw new Error('Streaming settings were saved but the read-back did not match');
    $('rocketAdminPassword').value = '';
    $('rocketHealthPassword').value = '';
    clearFormDirty([...Object.values(STREAMING_FEATURE_FIELDS), 'rocketAdminPassword', 'rocketHealthPassword']);
    const message = 'Verified: streaming feature policy matches the stored configuration.';
    setResult('streamingFeaturesResult', message, 'success');
    logActivity(message);
  } catch (error) {
    const message = errorMessage(error);
    setResult('streamingFeaturesResult', message, 'error');
    logActivity(`Streaming policy save failed: ${message}`, 'error');
  } finally {
    setBusy(false);
  }
}

function renderStreamingHealth() {
  const health = state.streamingHealth;
  if (!health) {
    $('streamingHealthState').textContent = 'Not checked';
    $('streamingHealthDetails').innerHTML = '<div class="empty-state">No origin health result is available.</div>';
    return;
  }
  const ok = health.ok === true;
  $('streamingHealthState').textContent = ok ? `Healthy (HTTP ${Number(health.status || 200)})` : 'Origin check failed';
  let body = String(health.body || health.message || 'The origin returned no detail.');
  try { body = JSON.stringify(JSON.parse(body), null, 2); } catch (_) { /* retain plain text */ }
  $('streamingHealthDetails').innerHTML = `
    <div class="record-row"><div class="record-copy"><b>${ok ? 'Origin accepted the authenticated health check' : escapeHtml(health.error_code || 'Origin unavailable')}</b><span>${escapeHtml(body.slice(0, 3000))}</span></div><div class="record-meta"><span>${ok ? 'OK' : 'FAILED'}</span></div></div>`;
}

async function loadStreamingHealth() {
  setResult('streamingHealthResult');
  try {
    state.streamingHealth = await api('/api/streaming/health');
    renderStreamingHealth();
    if (!state.streamingHealth?.ok) throw new Error(state.streamingHealth?.message || 'The origin health check failed');
    setResult('streamingHealthResult', 'Verified: the origin health endpoint responded with configured credentials.', 'success');
    return state.streamingHealth;
  } catch (error) {
    if (!state.streamingHealth) state.streamingHealth = { ok: false, message: errorMessage(error) };
    renderStreamingHealth();
    setResult('streamingHealthResult', errorMessage(error), 'error');
    throw error;
  }
}

function clearStreamingActionArms() {
  state.streamingActionArmed = {};
  [
    ['moveStreamingListenersButton', 'Arm listener move'],
    ['kickStreamingSourceButton', 'Arm source kick'],
    ['insertStreamingMidrollButton', 'Arm midroll insertion'],
  ].forEach(([id, label]) => {
    const button = $(id);
    if (!button) return;
    button.classList.remove('armed');
    button.textContent = label;
  });
}

async function confirmStreamingAction(action, button, execute) {
  const now = Date.now();
  if ((state.streamingActionArmed[action] || 0) <= now) {
    clearStreamingActionArms();
    state.streamingActionArmed[action] = now + 20000;
    button.classList.add('armed');
    button.textContent = `Confirm ${button.textContent.replace(/^Confirm /, '')}`;
    setResult('streamingManagementResult', `Click "${button.textContent}" again within 20 seconds.`, 'error');
    window.setTimeout(() => {
      if ((state.streamingActionArmed[action] || 0) <= Date.now()) clearStreamingActionArms();
    }, 20500);
    return;
  }
  clearStreamingActionArms();
  await execute();
}

function assertOriginManagementAccepted(result) {
  if (result?.ok !== true) throw new Error(result?.message || result?.error_code || 'The origin rejected the management request');
  return result;
}

async function runStreamingManagementAction(action) {
  const mount = selectedStreamingMount();
  if (!state.stationId) throw new Error('Select a station first');
  if (!mount) throw new Error('Enter the source mount');
  const payload = { station_id: Number(state.stationId), mount };
  let endpoint = '';
  let description = '';
  if (action === 'move') {
    const destinationInput = $('streamingDestination').value.trim();
    if (!destinationInput) throw new Error('Enter the listener destination mount');
    const destination = destinationInput.startsWith('/') ? destinationInput : `/${destinationInput}`;
    if (destination === mount) throw new Error('Source and destination mounts must be different');
    payload.destination = destination;
    endpoint = '/api/streaming/manage/move-listeners';
    description = `listeners moved from ${mount} to ${destination}`;
  } else if (action === 'kick') {
    endpoint = '/api/streaming/manage/kick';
    description = `source kicked from ${mount}`;
  } else if (action === 'midroll') {
    let ads;
    try { ads = JSON.parse($('streamingMidrollAds').value.trim()); } catch (_) { throw new Error('Midroll ads must be a valid JSON array'); }
    if (!Array.isArray(ads) || !ads.length || ads.some((item) => !item || typeof item !== 'object' || Array.isArray(item))) {
      throw new Error('Midroll ads must be a non-empty JSON array of objects');
    }
    payload.ads = ads;
    endpoint = '/api/streaming/manage/midroll';
    description = `midroll accepted for ${mount}`;
  } else {
    throw new Error('Unknown streaming management action');
  }
  setBusy(true, 'Contacting streaming origin...', `${action} on ${mount}`);
  setResult('streamingManagementResult');
  try {
    const result = assertOriginManagementAccepted(await api(endpoint, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
      timeoutMs: 30000,
    }));
    const message = `Verified: ${description}; origin returned HTTP ${Number(result.status || 200)}.`;
    setResult('streamingManagementResult', message, 'success');
    logActivity(message);
  } catch (error) {
    const message = errorMessage(error);
    setResult('streamingManagementResult', message, 'error');
    logActivity(`Streaming management failed: ${message}`, 'error');
  } finally {
    setBusy(false);
  }
}

function initializeComplianceDefaults() {
  const today = new Date(); const first = new Date(today.getFullYear(), today.getMonth(), 1); const previous = new Date(first); previous.setDate(0);
  $('musicUsageFrom').value = first.toISOString().slice(0, 10);
  $('musicUsageTo').value = today.toISOString().slice(0, 10);
  $('musicCloseYear').value = String(previous.getFullYear());
  $('musicCloseMonth').value = String(previous.getMonth() + 1);
}

async function loadOperatorViewData(view) {
  if (!state.stationId) return;
  if (view === 'onair') {
    try { await loadGuestRecordings(); } catch (error) { setResult('guestRecordingLibraryResult', errorMessage(error), 'error'); }
  }
  if (view === 'playlists') {
    try { await loadPlaylists(); } catch (error) { setResult('playlistEditorResult', errorMessage(error), 'error'); }
  }
  if (view === 'media') {
    try { await Promise.all([loadYtdlpWorkspace(), loadMetadataRules()]); } catch (error) { setResult('ytdlpResult', errorMessage(error), 'error'); }
  }
  if (view === 'stations') {
    try { await loadSpeakerMonitor(); } catch (error) { setResult('speakerMonitorResult', errorMessage(error), 'error'); }
  }
  if (view === 'automation') {
    try { await loadStartupSound(); } catch (error) { setResult('startupSoundResult', errorMessage(error), 'error'); }
  }
  if (!IS_RTAI_ONAIR && view === 'services' && !state.jukeLibrary) {
    try { await loadJukeLibrary({ busy: false }); } catch (_) { /* result is rendered in the Juke panel */ }
  }
  if (view === 'shows') {
    try { await loadShows(); } catch (error) { setResult('showResult', errorMessage(error), 'error'); }
  }
  if (view === 'compliance') {
    try { await Promise.all([loadMusicUsage(), loadMusicClosures()]); } catch (error) { setResult('musicUsageResult', errorMessage(error), 'error'); }
  }
  if (view === 'ads') {
    try { await loadAdvertising(); } catch (error) { setResult('adItemResult', errorMessage(error), 'error'); }
  }
  if (!IS_RTAI_ONAIR && view === 'settings') {
    try { await loadHlsSettings(); } catch (error) { setResult('hlsSettingsResult', errorMessage(error), 'error'); }
  }
  if (view === 'streaming') {
    try { await loadStreamingFeatures(); } catch (error) { setResult('streamingFeaturesResult', errorMessage(error), 'error'); }
    if (!IS_RTAI_ONAIR) {
      try { await loadQualityOutputs(); } catch (error) { setResult('qualityOutputsResult', errorMessage(error), 'error'); }
    }
    try { await loadStreamingHealth(); } catch (_) { /* rendered in the health panel */ }
  }
}

function startRefreshTimer() {
  stopRefreshTimer();
  state.refreshTimer = window.setInterval(() => {
    if (!state.busy && !document.hidden) Promise.all([loadCoreStatus(), loadQueue()]).then(() => setConnection('online', 'Backend connected')).catch(() => setConnection('offline', 'Connection failed'));
  }, 5000);
}

function stopRefreshTimer() {
  if (state.refreshTimer) window.clearInterval(state.refreshTimer);
  state.refreshTimer = null;
}

function bindEvents() {
  $('loginForm').addEventListener('submit', login);
  $('logoutButton').addEventListener('click', logout);
  $('continueSessionButton').addEventListener('click', recordUserActivity);
  ['pointerdown', 'keydown', 'touchstart'].forEach((eventName) => document.addEventListener(eventName, recordUserActivity, { passive: true }));
  $('refreshButton').addEventListener('click', () => refreshAll(false));
  $('queueRefreshButton').addEventListener('click', () => loadQueue().catch((error) => toast(errorMessage(error), 'error')));
  $('speakerMonitorForm').addEventListener('submit', saveSpeakerMonitor);
  $('startupSoundForm').addEventListener('submit', saveStartupSound);
  $('startupSoundUploadForm').addEventListener('submit', uploadStartupSound);
  $('startupSoundMode').addEventListener('change', toggleStartupSoundFields);
  $('playlistCreateForm').addEventListener('submit', createPlaylist);
  $('playlistAutoForm').addEventListener('submit', generatePlaylist);
  $('playlistSelect').addEventListener('change', () => { disarmPlaylistDelete(); loadPlaylists(Number($('playlistSelect').value || 0)).catch((error) => setResult('playlistEditorResult', errorMessage(error), 'error')); });
  $('playlistAddItemForm').addEventListener('submit', addPlaylistItem);
  $('playlistBulkForm').addEventListener('submit', bulkAddPlaylistItems);
  $('deletePlaylistButton').addEventListener('click', deleteSelectedPlaylist);
  $('refreshPlaylistsButton').addEventListener('click', () => loadPlaylists().catch((error) => setResult('playlistEditorResult', errorMessage(error), 'error')));
  $('stationSelect').addEventListener('change', async () => {
    if (state.emergency.active || state.emergency.starting) await stopEmergency('station changed');
    disarmStartBroadcast();
    disarmStopBroadcast();
    disarmStationDelete();
    disarmPlaylistDelete();
    disarmMetadataMaintenance();
    state.metadataRuleDeleteArmed = {};
    state.selectedPlaylistId = 0;
    state.playlistItems = [];
    resetShowDeleteArm();
    clearGuestRecordingDeleteArms();
    clearAdDeleteArms();
    state.selectedAdBreakSetId = 0;
    state.selectedAdCampaignId = 0;
    clearStreamingActionArms();
    ['streamingMount', 'streamingDestination', 'streamingMidrollAds'].forEach((id) => { $(id).value = ''; delete $(id).dataset.dirty; });
    clearFormDirty([
      'libraryFolder', 'libraryProfileLabel', 'libraryDefaultGenre', 'libraryDefaultLanguage', 'jingleFolder', 'jingleFolderReplace',
      'broadcastAutostartEnabled', 'autoplayShuffleSeed',
      'currentStationName', 'currentOutputGain', 'currentIcecastEnabled', 'currentIcecastHost', 'currentIcecastPort', 'currentIcecastMount', 'currentIcecastUser', 'currentIcecastPassword', 'currentIcecastProfile', 'currentSourceProtocol', 'currentIcecastTlsEnabled', 'currentLocalEnabled', 'currentOutputDevice',
      'aiConfigEnabled', 'aiLlmModel', 'aiTtsProvider', 'aiVoicePersona', 'aiTtsModelPath', 'aiMaxSeconds', 'aiStationInterval', 'aiIncludeHistory', 'aiEducational', 'aiPromptTemplate',
    ]);
    state.stationId = Number($('stationSelect').value);
    state.stationOutput = {};
    state.stationSettings = {};
    renderOutputConfiguration();
    $('workspaceStation').textContent = `Active: ${selectedStationName()}`;
    localStorage.setItem('radiotedu_onair_station_id', String(state.stationId));
    const url = new URL(window.location.href);
    url.searchParams.set('station_id', String(state.stationId));
    window.history.replaceState({}, '', url);
    state.libraryPage = 1;
    // The output form is the authoritative operator control. Load it first
    // instead of making it wait behind every diagnostics/media request.
    await loadSelectedStationOutput();
    await refreshAll(false);
    await loadOperatorViewData(state.activeView);
  });
  $('startBroadcastButton').addEventListener('click', startBroadcast);
  $('stopBroadcastButton').addEventListener('click', stopBroadcast);
  $('emergencyPresetButton').addEventListener('click', useEmergencyPreset);
  $('previewEmergencyButton').addEventListener('click', previewEmergencySource);
  $('startEmergencyButton').addEventListener('click', startEmergency);
  $('stopEmergencyButton').addEventListener('click', () => stopEmergency('operator stop'));
  $('enableAiButton').addEventListener('click', () => setAiEnabled(true));
  $('disableAiButton').addEventListener('click', () => setAiEnabled(false));
  $('stationForm').addEventListener('submit', createStation);
  $('configureIcecast').addEventListener('change', toggleIcecastFields);
  $('icecastProtocol').addEventListener('change', () => updateSourceProfileCompatibility('icecastProtocol', 'icecastProfile'));
  $('currentOutputForm').addEventListener('submit', saveCurrentOutput);
  $('testCurrentOutputButton').addEventListener('click', testCurrentOutput);
  $('deleteStationButton').addEventListener('click', deleteCurrentStation);
  $('currentIcecastEnabled').addEventListener('change', toggleCurrentOutputFields);
  $('currentLocalEnabled').addEventListener('change', toggleCurrentOutputFields);
  ['currentIcecastHost', 'currentIcecastPort', 'currentIcecastMount', 'currentIcecastProfile', 'currentSourceProtocol', 'currentIcecastTlsEnabled'].forEach((id) => {
    $(id)?.addEventListener('input', renderStreamWizardSummary);
    $(id)?.addEventListener('change', renderStreamWizardSummary);
  });
  $('currentSourceProtocol').addEventListener('change', toggleCurrentOutputFields);
  $('aiConfigForm').addEventListener('submit', saveAiConfiguration);
  $('testAiButton').addEventListener('click', runAiTest);
  $('campaignForm').addEventListener('submit', saveCampaign);
  $('previewCampaignNamesButton').addEventListener('click', () => normalizeCampaignNames(true));
  $('applyCampaignNamesButton').addEventListener('click', () => normalizeCampaignNames(false));
  $('resolveVotingRoundButton').addEventListener('click', resolveVotingRound);
  $('integrationForm').addEventListener('submit', saveIntegrations);
  $('testIntegrationsButton').addEventListener('click', testIntegrations);
  $('publishVotingRoundButton').addEventListener('click', publishVotingRound);
  $('serviceControlForm').addEventListener('submit', saveRadioTEDUServices);
  $('checkAllServicesButton').addEventListener('click', checkAllRadioTEDUServices);
  $('jukeLibraryUploadForm').addEventListener('submit', uploadJukeLibrarySongs);
  $('jukeLibrarySearchForm').addEventListener('submit', (event) => { event.preventDefault(); clearJukeLibraryActionArms(); loadJukeLibrary().catch(() => {}); });
  $('refreshJukeLibraryButton').addEventListener('click', () => { clearJukeLibraryActionArms(); loadJukeLibrary().catch(() => {}); });
  $('refreshReadinessButton').addEventListener('click', refreshReadiness);
  $('repairDependenciesButton').addEventListener('click', repairDependencies);
  $('reloadBackendButton').addEventListener('click', reloadBackendSafely);
  $('refreshWatchdogButton').addEventListener('click', () => refreshWatchdogStatus().catch((error) => setResult('watchdogResult', errorMessage(error), 'error')));
  $('repairWatchdogButton').addEventListener('click', () => repairWatchdogProblems().catch((error) => setResult('watchdogResult', errorMessage(error), 'error')));
  $('createDiagnosticBundleButton').addEventListener('click', createDiagnosticBundle);
  $('refreshDiagnosticBundlesButton').addEventListener('click', () => loadDiagnosticBundles().catch((error) => setResult('diagnosticBundleResult', errorMessage(error), 'error')));
  $('passwordForm').addEventListener('submit', changePassword);
  $('userAdminSelect').addEventListener('change', () => { state.userDeactivateArmedUntil = 0; renderUserAdminEditor(); setResult('userAdminResult'); });
  $('userAdminForm').addEventListener('submit', saveAdminUser);
  $('deactivateUserAdminButton').addEventListener('click', deactivateAdminUser);
  $('resetUserPasswordForm').addEventListener('submit', resetAdminUserPassword);
  $('roleAdminSelect').addEventListener('change', () => { state.roleDeactivateArmedUntil = 0; renderRoleAdminEditor(); setResult('roleAdminResult'); });
  $('roleAdminForm').addEventListener('submit', saveAdminRole);
  $('deactivateRoleAdminButton').addEventListener('click', deactivateAdminRole);
  $('stationName').addEventListener('input', () => {
    const mount = $('icecastMount');
    if (mount.dataset.edited === '1') return;
    const slug = $('stationName').value.trim().toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '');
    mount.value = `/${slug || 'new-station'}`;
  });
  $('icecastMount').addEventListener('input', () => { $('icecastMount').dataset.edited = '1'; });
  $('librarySearchForm').addEventListener('submit', (event) => { event.preventDefault(); loadLibrary(1).catch((error) => toast(errorMessage(error), 'error')); });
  $('ytdlpImportForm').addEventListener('submit', queueYtdlpImport);
  $('refreshYtdlpButton').addEventListener('click', () => loadYtdlpWorkspace().catch((error) => setResult('ytdlpResult', errorMessage(error), 'error')));
  $('metadataRuleForm').addEventListener('submit', createMetadataRule);
  $('metadataMaintenanceForm').addEventListener('submit', runMetadataMaintenance);
  $('metadataMaintenanceAction').addEventListener('change', disarmMetadataMaintenance);
  $('libraryFolderForm').addEventListener('submit', syncLibraryFolder);
  $('rescanLibraryButton').addEventListener('click', requestManagedLibraryRescan);
  $('refreshUnifiedMediaButton').addEventListener('click', refreshUnifiedMedia);
  $('broadcastAutostartEnabled').addEventListener('change', updateBroadcastAutostartFromControl);
  $('saveDeterministicPolicyButton').addEventListener('click', saveDeterministicPolicy);
  $('browseLibraryFolderButton').addEventListener('click', () => pickManagedFolder('libraryFolder', 'Select this station\'s music folder'));
  ['libraryFolder', 'libraryProfileLabel', 'libraryDefaultGenre', 'libraryDefaultLanguage'].forEach((id) => {
    $(id).addEventListener('input', () => { $(id).dataset.dirty = '1'; });
  });
  $('libraryPrev').addEventListener('click', () => loadLibrary(state.libraryPage - 1));
  $('libraryNext').addEventListener('click', () => loadLibrary(state.libraryPage + 1));
  $('jingleUploadForm').addEventListener('submit', uploadJingles);
  $('jingleFolderForm').addEventListener('submit', syncJingleFolder);
  $('browseJingleFolderButton').addEventListener('click', () => pickManagedFolder('jingleFolder', 'Select this station\'s jingle folder'));
  $('jingleFiles').addEventListener('change', () => { const count = $('jingleFiles').files.length; $('jingleFileLabel').textContent = count ? `${count} file${count === 1 ? '' : 's'} selected` : 'Choose one or more jingle files'; });
  $('sweeperForm').addEventListener('submit', saveSweeper);
  $('daypartForm').addEventListener('submit', saveDayparts);
  $('resetDaypartsButton').addEventListener('click', resetDayparts);
  $('scheduleForm').addEventListener('submit', createScheduleItem);
  $('recoveryForm').addEventListener('submit', createRecoveryPoint);
  $('refreshRecoveryButton').addEventListener('click', () => loadRecoveryPoints().catch((error) => setResult('recoveryResult', errorMessage(error), 'error')));
  $('showSelect').addEventListener('change', () => selectShow().catch((error) => setResult('showResult', errorMessage(error), 'error')));
  $('showForm').addEventListener('submit', saveShow);
  $('deleteShowButton').addEventListener('click', deleteShow);
  $('refreshShowsButton').addEventListener('click', () => loadShows(state.selectedShowId).catch((error) => setResult('showResult', errorMessage(error), 'error')));
  $('showGoLiveButton').addEventListener('click', () => runShowAction('live'));
  $('showGoBreakButton').addEventListener('click', () => runShowAction('break'));
  $('showEndButton').addEventListener('click', () => runShowAction('end'));
  $('showAssignmentForm').addEventListener('submit', assignShowUser);
  $('showAudioForm').addEventListener('submit', uploadShowAudio);
  $('musicUsageFilterForm').addEventListener('submit', (event) => { event.preventDefault(); loadMusicUsage().catch((error) => setResult('musicUsageResult', errorMessage(error), 'error')); });
  $('exportMusicUsageButton').addEventListener('click', exportMusicUsage);
  $('loadMusicMetadataButton').addEventListener('click', () => loadMusicMetadata().catch((error) => setResult('musicMetadataResult', errorMessage(error), 'error')));
  $('musicMetadataForm').addEventListener('submit', saveMusicMetadata);
  $('musicMonthlyCloseForm').addEventListener('submit', closeMusicMonth);
  $('refreshGuestRecordingsButton').addEventListener('click', () => loadGuestRecordings().catch((error) => setResult('guestRecordingLibraryResult', errorMessage(error), 'error')));
  window.addEventListener('radiotedu:guest-recordings-changed', () => loadGuestRecordings().catch((error) => setResult('guestRecordingLibraryResult', errorMessage(error), 'error')));
  $('adItemForm').addEventListener('submit', enqueueAdItem);
  $('refreshAdsButton').addEventListener('click', () => loadAdvertising().catch((error) => setResult('adItemResult', errorMessage(error), 'error')));
  $('adBreakSetSelect').addEventListener('change', () => { state.selectedAdBreakSetId = Number($('adBreakSetSelect').value || 0); clearAdDeleteArms(); renderAdBreakSetEditor(); setResult('adBreakSetResult'); });
  $('adBreakSetForm').addEventListener('submit', saveAdBreakSet);
  $('deleteAdBreakSetButton').addEventListener('click', () => deleteAdvertisingEntity('break-set'));
  $('adCampaignSelect').addEventListener('change', () => { state.selectedAdCampaignId = Number($('adCampaignSelect').value || 0); clearAdDeleteArms(); renderAdCampaignEditor(); setResult('adCampaignResult'); });
  $('adCampaignForm').addEventListener('submit', saveAdCampaign);
  $('deleteAdCampaignButton').addEventListener('click', () => deleteAdvertisingEntity('campaign'));
  $('hlsSettingsForm').addEventListener('submit', (event) => event.preventDefault());
  $('startHlsButton').addEventListener('click', startHls);
  $('stopHlsButton').addEventListener('click', stopHls);
  $('refreshHlsButton').addEventListener('click', refreshHlsStatus);
  $('startHlsHomeButton').addEventListener('click', startHls);
  $('refreshHlsHomeButton').addEventListener('click', refreshHlsStatus);
  $('streamingFeaturesForm').addEventListener('submit', saveStreamingFeatures);
  $('qualityOutputsForm').addEventListener('submit', saveQualityOutputs);
  $('prepare16MountPlanButton').addEventListener('click', prepareApproved16MountPlan);
  $('streamingManagementForm').addEventListener('submit', (event) => event.preventDefault());
  $('applyQualityOutputsButton').addEventListener('click', confirmQualityOutputApply);
  $('diagnoseQualityOutputsButton').addEventListener('click', () => diagnoseQualityOutputs().catch(() => {}));
  $('refreshQualityOutputsButton').addEventListener('click', () => loadQualityOutputs().then(() => setResult('qualityOutputsResult', 'Quality settings reloaded.', 'success')).catch((error) => setResult('qualityOutputsResult', errorMessage(error), 'error')));
  $('refreshStreamingHealthButton').addEventListener('click', () => loadStreamingHealth().catch(() => {}));
  $('moveStreamingListenersButton').addEventListener('click', () => confirmStreamingAction('move', $('moveStreamingListenersButton'), () => runStreamingManagementAction('move')).catch((error) => setResult('streamingManagementResult', errorMessage(error), 'error')));
  $('kickStreamingSourceButton').addEventListener('click', () => confirmStreamingAction('kick', $('kickStreamingSourceButton'), () => runStreamingManagementAction('kick')).catch((error) => setResult('streamingManagementResult', errorMessage(error), 'error')));
  $('insertStreamingMidrollButton').addEventListener('click', () => confirmStreamingAction('midroll', $('insertStreamingMidrollButton'), () => runStreamingManagementAction('midroll')).catch((error) => setResult('streamingManagementResult', errorMessage(error), 'error')));
  $('streamingMount').addEventListener('input', () => { $('streamingMount').dataset.dirty = '1'; $('streamingMountState').textContent = selectedStreamingMount() || 'No mount selected'; clearStreamingActionArms(); });
  $('streamingDestination').addEventListener('input', clearStreamingActionArms);
  $('streamingMidrollAds').addEventListener('input', clearStreamingActionArms);
  [
    'jingleFolder', 'jingleFolderReplace',
    'sweeperEnabled', 'sweeperInterval', 'sweeperMode',
    'broadcastAutostartEnabled', 'autoplayShuffleSeed',
      'currentStationName', 'currentOutputGain', 'currentIcecastEnabled', 'currentIcecastHost', 'currentIcecastPort', 'currentIcecastMount', 'currentIcecastUser', 'currentIcecastPassword', 'currentIcecastProfile', 'currentSourceProtocol', 'currentIcecastTlsEnabled', 'currentLocalEnabled', 'currentOutputDevice',
    'aiConfigEnabled', 'aiLlmModel', 'aiTtsProvider', 'aiVoicePersona', 'aiTtsModelPath', 'aiMaxSeconds', 'aiStationInterval', 'aiIncludeHistory', 'aiEducational', 'aiPromptTemplate',
    'campaignName', 'campaignStartsAt', 'campaignEndsAt', 'campaignEnabled', 'campaignVotingEnabled', 'campaignAiEnabled',
    'votingEnabled', 'votingBaseUrl', 'votingDeviceId', 'votingAgentToken', 'studyEnabled', 'studyBaseUrl',
    ...Object.values(STREAMING_FEATURE_FIELDS), 'rocketAdminPassword', 'rocketHealthPassword',
    'ytdlpAudioFormat', 'ytdlpAudioQuality', 'ytdlpDownloadPlaylist', 'ytdlpMusicOnly', 'ytdlpAutoTrim', 'ytdlpAutoIntro',
  ].forEach((id) => $(id).addEventListener('input', () => { $(id).dataset.dirty = '1'; }));
  $('clearActivityButton').addEventListener('click', () => { $('activityList').innerHTML = ''; });
  document.addEventListener('click', (event) => {
    const daypartAdd = event.target.closest('[data-daypart-add]');
    if (daypartAdd) addDaypartRule(daypartAdd.dataset.daypartAdd);
    const daypartRemove = event.target.closest('[data-daypart-remove]');
    if (daypartRemove) removeDaypartRule(daypartRemove.closest('[data-daypart-rule]'));
    const add = event.target.closest('[data-add-track]');
    if (add) addTrackToQueue(Number(add.dataset.addTrack));
    const queueButton = event.target.closest('[data-queue-action]');
    if (queueButton) queueAction(queueButton.dataset.queueAction, Number(queueButton.dataset.queueItemId));
    const skipButton = event.target.closest('[data-queue-skip]');
    if (skipButton) skipCurrentQueueItem(Number(skipButton.dataset.queueSkip));
    const serviceButton = event.target.closest('[data-service-action]');
    if (serviceButton) controlRadioTEDUService(serviceButton);
    const servicePathButton = event.target.closest('[data-service-path]');
    if (servicePathButton) pickRadioTEDUServicePath(servicePathButton);
    const jukeLibraryButton = event.target.closest('[data-juke-library-action]');
    if (jukeLibraryButton) controlJukeLibraryItem(jukeLibraryButton);
    const productRescan = event.target.closest('[data-product-catalog-rescan]');
    if (productRescan) requestProductCatalogRescan(productRescan.dataset.productCatalogRescan);
    const recoveryVerify = event.target.closest('[data-recovery-verify]');
    if (recoveryVerify) verifyRecoveryPoint(Number(recoveryVerify.dataset.recoveryVerify));
    const diagnosticDownload = event.target.closest('[data-diagnostic-download]');
    if (diagnosticDownload) downloadDiagnosticBundle(diagnosticDownload.dataset.diagnosticDownload).catch((error) => setResult('diagnosticBundleResult', errorMessage(error), 'error'));
    const showUnassign = event.target.closest('[data-show-unassign]');
    if (showUnassign) unassignShowUser(Number(showUnassign.dataset.showUnassign));
    const musicTrack = event.target.closest('[data-music-track]');
    if (musicTrack) {
      activateOperatorView('compliance', { focus: false });
      loadMusicMetadata(Number(musicTrack.dataset.musicTrack)).catch((error) => setResult('musicMetadataResult', errorMessage(error), 'error'));
    }
    const guestRecordingDownload = event.target.closest('[data-guest-recording-download]');
    if (guestRecordingDownload) downloadGuestRecording(Number(guestRecordingDownload.dataset.guestRecordingDownload)).catch((error) => setResult('guestRecordingLibraryResult', errorMessage(error), 'error'));
    const guestRecordingDelete = event.target.closest('[data-guest-recording-delete]');
    if (guestRecordingDelete) deleteGuestRecording(Number(guestRecordingDelete.dataset.guestRecordingDelete));
    const playlistMove = event.target.closest('[data-playlist-move]');
    if (playlistMove) movePlaylistItem(Number(playlistMove.dataset.playlistItem), playlistMove.dataset.playlistMove);
    const playlistRemove = event.target.closest('[data-playlist-remove]');
    if (playlistRemove) removePlaylistItem(Number(playlistRemove.dataset.playlistRemove));
    const metadataRuleToggle = event.target.closest('[data-metadata-rule-toggle]');
    if (metadataRuleToggle) toggleMetadataRule(Number(metadataRuleToggle.dataset.metadataRuleToggle));
    const metadataRuleDelete = event.target.closest('[data-metadata-rule-delete]');
    if (metadataRuleDelete) deleteMetadataRule(Number(metadataRuleDelete.dataset.metadataRuleDelete));
  });
}

async function boot() {
  await loadProductEditionProfile();
  applyProductEdition();
  initializeComplianceDefaults();
  initializeAdDefaults();
  initializeOperatorNavigation();
  bindEvents();
  toggleIcecastFields();
  toggleCurrentOutputFields();
  setConnection('', 'Connecting');
  if (await ensureSignedIn()) {
    try { await showApp(); } catch (error) { toast(errorMessage(error), 'error'); showLogin(); }
  } else {
    showLogin();
  }
}

window.addEventListener('DOMContentLoaded', boot);
window.addEventListener('pagehide', emergencyPageHideCleanup);
