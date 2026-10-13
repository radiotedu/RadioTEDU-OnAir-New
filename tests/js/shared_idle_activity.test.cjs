const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const { appJs, section } = require('./unified_test_helpers.cjs');

function tab(storage, clock, identity = 1) {
  const nodes = new Map();
  const state = { lastUserActivityAt: clock.now, idleTimer: null };
  let tick;
  let revoked = 0;
  const context = { state, Date: { now: () => clock.now }, IDLE_TIMEOUT_MS: 900000, IDLE_WARNING_MS: 60000,
    OPERATOR_ACTIVITY_KEY: 'activity', currentUser: () => ({ id: identity }),
    localStorage: { getItem: k => storage.get(k) || null, setItem: (k, v) => storage.set(k, v) },
    $: id => { if (!nodes.has(id)) nodes.set(id, { hidden: id !== 'appShell', textContent: '' }); return nodes.get(id); },
    window: { setInterval: fn => { tick = fn; return 1; }, clearInterval: () => {} },
    api: async () => {}, clearSession: () => { revoked++; }, showLogin: () => {} };
  vm.runInNewContext(section(appJs, 'function sharedOperatorActivityAt(', 'async function logout('), context);
  return { context, tick: () => tick(), revoked: () => revoked };
}

test('activity in one tab prevents an idle sibling from revoking the shared session', async () => {
  const storage = new Map(); const clock = { now: 1000 };
  const a = tab(storage, clock), b = tab(storage, clock);
  a.context.startIdleTimer(); b.context.startIdleTimer();
  clock.now += 14 * 60000; b.context.recordUserActivity();
  clock.now += 2 * 60000; a.tick();
  await a.context.expireIdleSession();
  assert.equal(a.revoked(), 0);
});

test('the original fifteen-minute lock still applies when all tabs are truly idle', async () => {
  const storage = new Map(); const clock = { now: 1000 }; const a = tab(storage, clock);
  a.context.startIdleTimer(); clock.now += 900001;
  await a.context.expireIdleSession();
  assert.equal(a.revoked(), 1);
});

test('another identity and future timestamps do not extend a session', async () => {
  const storage = new Map(); const clock = { now: 1000 }; const a = tab(storage, clock);
  a.context.startIdleTimer(); clock.now += 900001;
  storage.set('activity', JSON.stringify({ identity: '2', at: clock.now }));
  assert.equal(a.context.sharedOperatorActivityAt(), 0);
  storage.set('activity', JSON.stringify({ identity: '1', at: clock.now + 1 }));
  assert.equal(a.context.sharedOperatorActivityAt(), 0);
  await a.context.expireIdleSession();
  assert.equal(a.revoked(), 1);
});

test('an activity race immediately before expiry is rechecked before logout', async () => {
  const storage = new Map(); const clock = { now: 1000 }; const a = tab(storage, clock), b = tab(storage, clock);
  a.context.startIdleTimer(); clock.now += 900001; b.context.recordUserActivity();
  await a.context.expireIdleSession();
  assert.equal(a.revoked(), 0);
});
