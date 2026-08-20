import os
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(os.name != "nt", reason="Windows ACL contract")
def test_data_acl_helper_removes_builtin_users_access_recursively(tmp_path):
    root = tmp_path / "OnAir"
    paths = [
        root / "cleanroom.db",
        root / "secrets" / "jwt-secret.key",
        root / "Logs" / "backend.log",
        root / "Media" / "Songs" / "track.mp3",
    ]
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"acl-test")

    hardening = subprocess.run(
        [
            "icacls.exe",
            str(root),
            "/grant",
            "*S-1-5-32-545:(OI)(CI)(M)",
            "/T",
            "/C",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert hardening.returncode == 0, hardening.stderr or hardening.stdout

    repository_root = Path(__file__).resolve().parents[2]
    helper = repository_root / "installer" / "HardenServiceHostAcl.ps1"
    powershell = (
        Path(os.environ.get("WINDIR", r"C:\Windows"))
        / "System32"
        / "WindowsPowerShell"
        / "v1.0"
        / "powershell.exe"
    )
    hardening = subprocess.run(
        [
            str(powershell),
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(helper),
            "-OnAirRoot",
            str(root),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert hardening.returncode == 0, hardening.stderr or hardening.stdout

    verification = r"""
$securityModule = Join-Path $PSHOME 'Modules\Microsoft.PowerShell.Security\Microsoft.PowerShell.Security.psd1'
Import-Module -Name $securityModule -ErrorAction Stop
$Root = $env:RADIOTEDU_ACL_TEST_ROOT
$allowed = @(
  'S-1-5-18',
  'S-1-5-32-544',
  [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
)
$targets = @(Get-Item -LiteralPath $Root -Force) + @(
  Get-ChildItem -LiteralPath $Root -Recurse -Force
)
foreach ($target in $targets) {
  $acl = Get-Acl -LiteralPath $target.FullName
  if (-not $acl.AreAccessRulesProtected) { throw "inheritance enabled" }
  foreach ($entry in $acl.Access) {
    $sid = $entry.IdentityReference.Translate(
      [System.Security.Principal.SecurityIdentifier]
    ).Value
    if ($sid -notin $allowed) { throw "unexpected principal: $sid" }
  }
}
"""
    subprocess.run(
        [
            str(powershell),
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            verification,
        ],
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, "RADIOTEDU_ACL_TEST_ROOT": str(root)},
    )
