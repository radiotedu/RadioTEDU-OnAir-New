const test = require('node:test');
const assert = require('node:assert/strict');
const { html, appJs, section } = require('./unified_test_helpers.cjs');

test('stations workspace exposes create, output, test, and deletion controls', () => {
  for (const id of ['stationForm', 'stationName', 'icecastProtocol', 'currentOutputForm', 'currentSourceProtocol', 'currentIcecastHost', 'testCurrentOutputButton', 'deleteStationButton']) {
    assert.match(html, new RegExp(`id="${id}"`));
  }
});

test('station creation configures output and verifies backend read-back', () => {
  const source = section(appJs, 'async function createStation(', 'function toggleCurrentOutputFields(');
  assert.match(source, /api\('\/api\/stations'/);
  assert.match(source, /api\('\/api\/stations\/output'/);
  assert.match(source, /source_protocol: sourceProtocol/);
  assert.match(source, /createdId/);
  assert.match(source, /await poll\(async \(\) =>/);
  assert.match(source, /description: 'new station and output configuration'/);
  assert.match(source, /method: 'DELETE'/);
});

test('station output saves are permission-gated and read back before success', () => {
  const payload = section(appJs, 'function currentOutputPayload(', 'function outputMatches(');
  const save = section(appJs, 'async function saveCurrentOutput(', 'async function testCurrentOutput(');
  assert.match(payload, /icecast_password/);
  assert.match(payload, /source_protocol: sourceProtocol/);
  assert.match(appJs, /SHOUTcast legacy source/);
  assert.match(save, /api\(`\/api\/stations\/\$\{state\.stationId\}`/);
  assert.match(save, /createAndValidateStreamDraft/);
  assert.match(save, /Idempotency-Key/);
  assert.match(save, /await loadStations\(state\.stationId\)/);
  assert.match(appJs, /stream\.configure_advanced/);
});

test('station deletion is confirmed and cannot remove the final station', () => {
  const source = section(appJs, 'async function deleteCurrentStation(', 'async function addTrackToQueue(');
  assert.match(source, /state\.stations\.length <= 1/);
  assert.match(source, /stationDeleteArmedUntil <= Date\.now\(\)/);
  assert.match(source, /verifiedMutation/);
  assert.match(source, /verified: !\(stations\.stations \|\| \[\]\)\.some/);
});
