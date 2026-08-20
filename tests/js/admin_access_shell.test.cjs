const test = require('node:test');
const assert = require('node:assert/strict');
const { html, appJs, section } = require('./unified_test_helpers.cjs');

test('settings contains integrated account and least-privilege administration', () => {
  for (const id of ['passwordForm', 'userAdminPanel', 'userAdminForm', 'roleAdminPanel', 'roleAdminForm']) {
    assert.match(html, new RegExp(`id="${id}"`));
  }
  assert.doesNotMatch(html, /initial-admin-password\.txt|%ProgramData%\\RadioTEDU\\OnAir/);
});

test('admin panels use effective permissions and remain hidden from unauthorized view activation', () => {
  const visibility = section(appJs, 'function operatorHasPermission', 'function selectedAdminUser');
  const activation = section(appJs, 'function activateOperatorView', 'function initializeOperatorNavigation');
  const loader = section(appJs, 'async function loadAdminAccess', 'function selectedUserRoleTemplateIds');
  assert.match(visibility, /effective_permissions/);
  assert.match(activation, /dataset\.accessHidden === 'true'/);
  assert.match(loader, /users\.manage/);
  assert.match(loader, /users\.reset_password/);
  assert.match(loader, /roles\.manage/);
});

test('advanced stream destinations remain permission-gated', () => {
  const render = section(appJs, 'function renderOutputConfiguration', 'function renderAiConfiguration');
  assert.match(render, /stream\.configure_advanced/);
  assert.match(render, /streamAdvancedSettings.*hidden/);
  assert.match(render, /readOnly = !canUseAdvanced/);
});
