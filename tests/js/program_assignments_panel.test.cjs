const test = require('node:test');
const assert = require('node:assert/strict');
const { html, appJs, section } = require('./unified_test_helpers.cjs');

test('scheduler workspace exposes exact-time track event controls', () => {
  for (const id of ['scheduleForm', 'scheduleTrackId', 'schedulePlayAt', 'scheduleWindowEnd', 'scheduleItems']) {
    assert.match(html, new RegExp(`id="${id}"`));
  }
});

test('scheduled events are always scoped to the selected station', () => {
  const load = section(appJs, 'async function loadScheduleItems(', 'async function createScheduleItem(');
  const create = section(appJs, 'async function createScheduleItem(', 'async function loadRecoveryPoints(');
  assert.match(load, /station_id=\$\{state\.stationId\}/);
  assert.match(create, /station_id: state\.stationId/);
  assert.match(create, /track_id:/);
  assert.match(create, /play_at:/);
  assert.match(create, /window_end:/);
  assert.match(create, /await loadScheduleItems\(\)/);
});

test('seven-day program clock editor remains independently editable', () => {
  assert.match(html, /id="daypartForm"/);
  assert.match(html, /Save and verify weekly schedule/);
  assert.match(appJs, /Validating all seven 24-hour program clocks/);
  assert.match(appJs, /data-daypart-add/);
});
