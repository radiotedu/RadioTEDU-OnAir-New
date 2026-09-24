const test = require('node:test');
const assert = require('node:assert/strict');
const { appJs, section } = require('./unified_test_helpers.cjs');

test('Ads console uses a bounded request and a single short transport retry', () => {
  const load = section(appJs, 'async function loadAdvertising(', 'async function enqueueAdItem(');
  assert.match(load, /api\(`\/api\/ads\/console\?station_id=\$\{stationId\}&limit=50`,\s*\{[\s\S]*?timeoutMs:\s*8000,[\s\S]*?transportAttempts:\s*2/);
});

test('safe backend reload uses a visible two-click confirmation instead of a blocking browser prompt', () => {
  const reload = section(appJs, 'async function reloadBackendSafely(', 'async function changePassword(');
  assert.doesNotMatch(reload, /window\.confirm\(/);
  assert.match(reload, /backendReloadArmedUntil/);
  assert.match(reload, /Confirm safe backend reload/);
  assert.ok(reload.indexOf('Confirm safe backend reload') < reload.indexOf("api('/api/maintenance/backend/reload'"));
});
