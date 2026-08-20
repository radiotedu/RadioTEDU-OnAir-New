const test = require('node:test');
const assert = require('node:assert/strict');
const { html, appJs, guestRoomJs, section } = require('./unified_test_helpers.cjs');

test('guest room can acquire and retain a station-scoped operator studio', () => {
  const ownership = section(appJs, 'async function ensureOperatorStudioOwnership(', 'function resampleToPcm16(');
  assert.match(ownership, /\/api\/studios\?station_id=\$\{stationId\}/);
  assert.match(ownership, /\/api\/studios\/\$\{Number\(studio\.id\)\}\/join/);
  assert.match(ownership, /state\.joinedStudioId = Number\(verified\.id\)/);
  assert.match(guestRoomJs, /await ensureOperatorStudioOwnership\(Number\(state\.stationId\)\)/);
});

test('guest admission is off-air by default and every live audio action is explicit', () => {
  assert.match(html, /Everyone enters the lobby off-air/);
  assert.match(guestRoomJs, /data-guest-action="admit"/);
  assert.match(guestRoomJs, /data-guest-action="onair"/);
  assert.match(guestRoomJs, /guest-room\/all-off-air/);
  assert.doesNotMatch(guestRoomJs, /fetch\(/);
});

test('talkback is push-to-talk and releases browser audio tracks', () => {
  assert.match(guestRoomJs, /getUserMedia\(\{ audio:/);
  assert.match(guestRoomJs, /talkback\/start/);
  assert.match(guestRoomJs, /talkback\/stop/);
  assert.match(guestRoomJs, /getTracks\(\)\.forEach\(\(track\) => track\.stop\(\)\)/);
  assert.match(guestRoomJs, /\['pointerup', 'pointercancel', 'pointerleave', 'touchend'\]/);
});

test('station changes clear studio ownership so guests cannot leak across stations', () => {
  const load = section(appJs, 'async function loadStations(', 'async function loadCoreStatus(');
  assert.match(load, /previousStationId !== Number\(state\.stationId\)/);
  assert.match(load, /state\.studios = \[\]/);
  assert.match(load, /state\.joinedStudioId = 0/);
});
