const test = require('node:test');
const assert = require('node:assert/strict');
const { appJs, section } = require('./unified_test_helpers.cjs');

test('authenticated API wrapper attaches bearer token and performs one refresh retry', () => {
  const api = section(appJs, 'async function api(', 'async function poll(');
  assert.match(api, /headers\.set\('Authorization', `Bearer \$\{token\}`\)/);
  assert.match(api, /response\.status === 401 && retry && await refreshSession\(\)/);
  assert.match(api, /return api\(url, options, false\)/);
  assert.match(api, /return JSON\.parse\(text\)/);
});

test('boot validates the stored session before loading station state', () => {
  const boot = section(appJs, 'async function boot(', "window.addEventListener('DOMContentLoaded'");
  assert.ok(boot.indexOf('await ensureSignedIn()') < boot.indexOf('await showApp()'));
  assert.match(boot, /showLogin\(\)/);
  const validation = section(appJs, 'async function ensureSignedIn(', 'async function login(');
  assert.match(validation, /await api\('\/api\/auth\/me'\)/);
  assert.match(validation, /clearSession\(\)/);
});

test('login and logout never put credentials in URLs or logs', () => {
  const login = section(appJs, 'async function login(', 'async function showApp(');
  const logout = section(appJs, 'async function logout(', 'async function loadStations(');
  assert.match(login, /body: JSON\.stringify\(\{ username:/);
  assert.match(login, /saveSession\(JSON\.parse\(text\)\)/);
  assert.doesNotMatch(login, /console\.|location\.|password=.*[?&]/);
  assert.match(logout, /api\('\/api\/auth\/logout'/);
  assert.match(logout, /clearSession\(\)/);
  assert.match(logout, /showLogin\(\)/);
});
