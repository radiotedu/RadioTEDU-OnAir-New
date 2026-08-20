const test = require('node:test');
const assert = require('node:assert/strict');
const { appJs, section } = require('./unified_test_helpers.cjs');

test('operator status does not depend on a browser websocket remaining connected', () => {
  assert.doesNotMatch(appJs, /new WebSocket\(/);
  const polling = section(appJs, 'function startRefreshTimer(', 'function stopRefreshTimer(');
  assert.match(polling, /!state\.busy && !document\.hidden/);
  assert.match(polling, /loadCoreStatus\(\)/);
  assert.match(polling, /loadQueue\(\)/);
  assert.match(polling, /Connection failed/);
});

test('HTTP control transport is bounded and retries only safe operations', () => {
  const raw = section(appJs, 'async function rawFetch(', 'function saveSession(');
  const api = section(appJs, 'async function api(', 'async function poll(');
  assert.match(raw, /AbortController/);
  assert.match(raw, /controller\.abort\(\)/);
  assert.match(api, /method === 'GET' \|\| options\.idempotent/);
  assert.match(api, /\[502, 503, 504\]/);
  assert.match(api, /350 \* \(attempt \+ 1\)/);
});
