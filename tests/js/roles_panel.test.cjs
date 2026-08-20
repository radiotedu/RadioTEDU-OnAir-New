const test = require('node:test');
const assert = require('node:assert/strict');
const { html, appJs, section } = require('./unified_test_helpers.cjs');

test('role administration renders the backend permission catalog in Settings', () => {
  for (const id of ['roleAdminSelect', 'roleAdminName', 'roleAdminDescription', 'rolePermissionChoices']) {
    assert.match(html, new RegExp(`id="${id}"`));
  }
  const render = section(appJs, 'function renderRolePermissionChoices(', 'function renderRoleAdminEditor(');
  assert.match(render, /state\.permissionGroups/);
  assert.match(render, /data-role-permission/);
  assert.match(render, /role\?\.is_system/);
});

test('custom role saves use the known permission keys and verified read-back', () => {
  const save = section(appJs, 'async function saveAdminRole(', 'async function deactivateAdminRole(');
  assert.match(save, /permission_keys: selectedRolePermissionKeys\(\)/);
  assert.match(save, /\/api\/roles/);
  assert.match(save, /await loadAdminAccess\(true/);
  assert.match(save, /Role template read-back did not match/);
});

test('system roles cannot be edited and custom role deactivation is two-step', () => {
  const editor = section(appJs, 'function renderRoleAdminEditor(', 'function renderAdminAccess(');
  const deactivate = section(appJs, 'async function deactivateAdminRole(', 'function emergencyRecovery(');
  assert.match(editor, /operatorHasPermission\('roles\.manage'\) && !role\?\.is_system/);
  assert.match(deactivate, /if \(!role \|\| role\.is_system\) return/);
  assert.match(deactivate, /roleDeactivateArmedUntil <= Date\.now\(\)/);
  assert.match(deactivate, /remained active after deactivation/);
});
