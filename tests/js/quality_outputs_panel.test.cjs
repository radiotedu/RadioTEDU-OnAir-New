const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const root = path.resolve(__dirname, '..', '..');
const html = fs.readFileSync(path.join(root, 'app', 'static', 'onair', 'index.html'), 'utf8');
const app = fs.readFileSync(path.join(root, 'app', 'static', 'onair', 'app.js'), 'utf8');

test('Streaming exposes the approved 14-local plus 2-external mount plan', () => {
  assert.match(html, /id="qualityOutputsPanel"/);
  assert.match(html, /8 ADDITIONAL OUTPUTS/);
  assert.match(html, /six suffix-free music mounts/);
  assert.match(html, /`\/en` and `\/fr` remain the two independent AI streams/);
  assert.match(app, /expected\.channels\.length !== 6/);
  assert.match(app, /\/api\/streaming\/quality-outputs/);
  assert.match(html, /id="applyQualityOutputsButton"/);
  assert.match(html, /id="diagnoseQualityOutputsButton"/);
  assert.match(html, /id="prepare16MountPlanButton"/);
  assert.match(html, /id="qualityOriginSourceCapacity"/);
});

test('quality settings save and verify read-back without editable credentials', () => {
  assert.match(html, /inherits protected source credentials/);
  assert.match(html, /AAC-LC 192 Normal/);
  assert.match(app, /qualityOutputsMatch\(expected, stored\)/);
  assert.match(app, /protected credentials were not copied/);
  assert.doesNotMatch(html, /data-quality-(?:password|host|user|mount)-input/);
});

test('quality variants expose enable and public settings while canonical codec targets stay visible', () => {
  assert.match(app, /data-quality-enabled/);
  assert.match(app, /data-quality-public/);
  assert.match(app, /variant\.codec/);
  assert.match(app, /variant\.bitrate_kbps/);
  assert.match(html, /suffix-free station mount uses AAC-LC at 192 kbps/);
  assert.match(html, /HE-AAC v2 64 Low/);
  assert.match(html, /only Classical plus Cazz add lossless FLAC/);
});

test('operators can apply and diagnose quality outputs without Codex or exposed secrets', () => {
  assert.match(app, /\/api\/streaming\/quality-outputs\/apply/);
  assert.match(app, /\/api\/streaming\/quality-outputs\/diagnostics/);
  assert.match(app, /Confirm quality apply/);
  assert.match(app, /FFmpeg libfdk_aac: AAC-LC 192 Normal and HE-AAC v2 64 Low/);
  assert.match(app, /observed_healthy_local_mounts/);
  assert.match(app, /verified by audio delivery/);
  assert.match(app, /Configuration is valid, but delivery is incomplete/);
  assert.doesNotMatch(app, /quality_outputs_external_.*credential_configured/);
});
