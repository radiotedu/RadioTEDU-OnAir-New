const test = require('node:test');
const assert = require('node:assert/strict');
const { html, appJs, section } = require('./unified_test_helpers.cjs');

test('authenticated operator shell locks after fifteen minutes with a final-minute warning', () => {
  assert.match(appJs, /const IDLE_TIMEOUT_MS = 15 \* 60 \* 1000/);
  assert.match(appJs, /const IDLE_WARNING_MS = 60 \* 1000/);
  assert.match(html, /id="idleTimeoutBanner"[^>]*hidden/);
  assert.match(html, /id="continueSessionButton"/);
});

test('idle lifecycle starts only in the authenticated shell and stops at sign-in gate', () => {
  const showApp = section(appJs, 'async function showApp(', 'function showLogin(');
  const showLogin = section(appJs, 'function showLogin(', 'function recordUserActivity(');
  assert.match(showApp, /startIdleTimer\(\)/);
  assert.match(showLogin, /stopIdleTimer\(\)/);
});

test('activity hides the warning and true expiry revokes local session', () => {
  const activity = section(appJs, 'function recordUserActivity(', 'async function expireIdleSession(');
  const expiry = section(appJs, 'async function expireIdleSession(', 'function startIdleTimer(');
  const timer = section(appJs, 'function startIdleTimer(', 'function stopIdleTimer(');
  assert.match(activity, /lastUserActivityAt = Date\.now\(\)/);
  assert.match(activity, /idleTimeoutBanner.*hidden = true/);
  assert.match(timer, /remaining <= IDLE_WARNING_MS/);
  assert.match(timer, /expireIdleSession\(\)/);
  assert.match(expiry, /api\('\/api\/auth\/logout'/);
  assert.match(expiry, /clearSession\(\)/);
});

test('keyboard, pointer, touch, and continue-button activity reset the timer', () => {
  assert.match(appJs, /continueSessionButton.*recordUserActivity/);
  assert.match(appJs, /\['pointerdown', 'keydown', 'touchstart'\]/);
});
