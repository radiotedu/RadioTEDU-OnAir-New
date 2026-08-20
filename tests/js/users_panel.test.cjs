const test = require('node:test');
const assert = require('node:assert/strict');
const { html, appJs, section } = require('./unified_test_helpers.cjs');

test('operator account editor exposes create, role assignment, disable, and reset controls', () => {
  for (const id of ['userAdminSelect', 'userAdminUsername', 'userAdminDisplayName', 'userRoleTemplateChoices', 'deactivateUserAdminButton', 'resetUserPasswordForm']) {
    assert.match(html, new RegExp(`id="${id}"`));
  }
  assert.match(html, /userAdminPassword" type="password" minlength="8"/);
});

test('user create and update send explicit role template ids and verify read-back', () => {
  const save = section(appJs, 'async function saveAdminUser(', 'async function deactivateAdminUser(');
  assert.match(save, /role_template_ids: roleTemplateIds/);
  assert.match(save, /api\('\/api\/users'/);
  assert.match(save, /api\(`\/api\/users\/\$\{Number\(existing\.id\)\}`/);
  assert.match(save, /await loadAdminAccess\(true/);
  assert.match(save, /Operator account read-back did not match/);
});

test('account deactivation is confirmed, verified, and cannot target the signed-in operator', () => {
  const source = section(appJs, 'async function deactivateAdminUser(', 'async function resetAdminUserPassword(');
  assert.match(source, /Number\(user\.id\) === Number\(currentUser\(\)\.id\)/);
  assert.match(source, /userDeactivateArmedUntil <= Date\.now\(\)/);
  assert.match(source, /remained active after deactivation/);
});

test('password resets enforce minimum length and never echo the temporary value', () => {
  const source = section(appJs, 'async function resetAdminUserPassword(', 'function selectedRolePermissionKeys(');
  assert.match(source, /newPassword\.length < 8/);
  assert.match(source, /reset-password/);
  assert.match(source, /\$\('resetUserPassword'\)\.value = ''/);
  assert.match(source, /temporary value is not displayed or logged/);
  assert.doesNotMatch(source, /console\./);
});
