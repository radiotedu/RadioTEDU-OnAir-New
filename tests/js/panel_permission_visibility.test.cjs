const test = require('node:test');
const assert = require('node:assert/strict');
const { html, appJs, section } = require('./unified_test_helpers.cjs');

const expectedViews = [
  'onair', 'media', 'playlists', 'automation', 'emergency', 'services', 'settings', 'diagnostics',
  'stations', 'queue', 'scheduler', 'dayparting', 'shows', 'compliance', 'ads', 'streaming', 'recovery',
];

test('navigation exposes every required RadioTEDU workspace', () => {
  const views = [...html.matchAll(/data-operator-nav="([^"]+)"/g)].map((match) => match[1]);
  assert.deepEqual(views, expectedViews);
  for (const view of expectedViews) assert.match(html, new RegExp(`data-operator-view="${view}"`));
});

test('view activation displays only the selected and access-authorized fragments', () => {
  const source = section(appJs, 'function activateOperatorView(', 'function initializeOperatorNavigation(');
  assert.match(source, /node\.dataset\.operatorView !== view \|\| node\.dataset\.accessHidden === 'true'/);
  assert.match(source, /aria-current/);
  assert.match(source, /localStorage\.setItem\('radiotedu_onair_active_view'/);
  assert.match(source, /url\.hash = view/);
});
