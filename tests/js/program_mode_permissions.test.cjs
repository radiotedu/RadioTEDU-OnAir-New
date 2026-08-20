const test = require('node:test');
const assert = require('node:assert/strict');
const { appJs, section } = require('./unified_test_helpers.cjs');

test('broadcast start requires two clicks and end-to-end listener evidence', () => {
  const source = section(appJs, 'async function startBroadcast(', 'async function stopBroadcast(');
  assert.match(source, /state\.startArmedUntil <= Date\.now\(\)/);
  assert.match(source, /return;/);
  assert.match(source, /verifiedMutation/);
  assert.match(source, /health\.engine_running/);
  assert.match(source, /runtime\.output_feed_active/);
  assert.match(source, /isBroadcastVerifiedLive\(publicStation\)/);
});

test('broadcast stop requires confirmation and verifies preserved scheduler state', () => {
  const source = section(appJs, 'async function stopBroadcast(', 'async function refreshReadiness(');
  assert.match(source, /state\.stopArmedUntil <= Date\.now\(\)/);
  assert.match(source, /keep playlist/);
  assert.match(source, /operator-stop/);
  assert.match(source, /!runtime\?\.running && !runtime\?\.worker_loop\?\.running/);
});

test('emergency takeover has a separate bounded arming state and deterministic restore', () => {
  const arm = section(appJs, 'function armEmergencyTakeover(', 'function useEmergencyPreset(');
  const start = section(appJs, 'async function startEmergency(', 'async function stopEmergency(');
  const stop = section(appJs, 'async function stopEmergency(', 'function emergencyPageHideCleanup(');
  assert.match(arm, /armedUntil = Date\.now\(\) \+ 20000/);
  assert.match(start, /ensureOperatorStudioOwnership/);
  assert.match(start, /program_music_mode: 'mute'/);
  assert.match(stop, /normal program restoration/);
  assert.match(stop, /clearEmergencyRecovery\(\)/);
});
