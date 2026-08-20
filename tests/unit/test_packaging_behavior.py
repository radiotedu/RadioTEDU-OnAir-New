import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


def test_release_bundle_cli_emits_manifest_json(tmp_path):
    root = Path(__file__).resolve().parents[2]
    script = root / "scripts" / "release_bundle.py"
    version = (root / "VERSION").read_text(encoding="utf-8").strip()

    result = subprocess.run(
        [sys.executable, str(script), version],
        cwd=root,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr

    manifest = json.loads(result.stdout)
    assert manifest["version"] == version
    assert manifest["layout"] == {
        "backend_dir": "dist/backend",
        "desktop_dir": "dist/desktop",
        "desktop_shell_dir": "dist/desktop/shell",
        "desktop_supervisor_dir": "dist/desktop/supervisor",
    }
    assert {item["name"] for item in manifest["artifacts"]} == {
        "RadioTEDU-OnAir-Backend.exe",
        "RadioTEDU-OnAir.exe",
        "RadioTEDU-OnAir-Supervisor.exe",
    }


@pytest.mark.parametrize(
    "layout_parts",
    [
        ("dist", "backend-20260327-101500"),
        ("dist-20260327-101500", "backend"),
    ],
)
def test_portable_release_discovers_timestamped_backend_outputs(tmp_path, layout_parts):
    root = Path(__file__).resolve().parents[2]
    script_root = tmp_path / "portable"
    script_root.mkdir()

    shutil.copy2(root / "package_portable_release.ps1", script_root / "package_portable_release.ps1")

    exe_dir = script_root.joinpath(*layout_parts)
    exe_dir.mkdir(parents=True, exist_ok=True)
    backend_exe = exe_dir / "RadioTEDU-OnAir-Backend.exe"
    backend_exe.write_bytes(b"timestamped-backend")

    release_root = tmp_path / "release"
    last_release_path_file = tmp_path / "last_release_path.txt"

    result = subprocess.run(
        [
            "powershell",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script_root / "package_portable_release.ps1"),
            "-SkipBuild",
            "-ReleaseRoot",
            str(release_root),
            "-LastReleasePathFile",
            str(last_release_path_file),
        ],
        cwd=script_root,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr

    release_dirs = list(release_root.glob("radiotedu-broadcast-room-portable-*"))
    assert len(release_dirs) == 1

    copied_exe = release_dirs[0] / "RadioTEDU-OnAir-Backend.exe"
    assert copied_exe.read_bytes() == b"timestamped-backend"
    assert last_release_path_file.read_text(encoding="utf-8").strip() == str(
        copied_exe.resolve()
    )


def test_build_backend_onefile_fails_when_pyinstaller_reports_success_without_exe(tmp_path):
    root = Path(__file__).resolve().parents[2]
    script_root = tmp_path / "backend"
    script_root.mkdir()

    shutil.copy2(root / "build_backend_onefile.ps1", script_root / "build_backend_onefile.ps1")
    shutil.copy2(root / "requirements.lock", script_root / "requirements.lock")
    shutil.copy2(root / "VERSION", script_root / "VERSION")
    (script_root / "app").mkdir()
    (script_root / "app" / "__init__.py").write_text("", encoding="utf-8")
    (script_root / "run_cleanroom.py").write_text("", encoding="utf-8")

    tools_dir = script_root / "tools"
    tools_dir.mkdir()
    for tool_name in ("ffmpeg", "ffplay", "ffprobe"):
        (tools_dir / f"{tool_name}.cmd").write_text("@echo off\nexit /b 0\n", encoding="utf-8")

    fake_python = script_root / "fake_python.ps1"
    fake_python.write_text(
        """
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Args
)

$command = $Args -join ' '

if ($Args.Count -ge 2 -and $Args[0] -eq '-c') {
    if ($command -match 'sys.version_info\\[1\\]') {
        Write-Output '3.12'
    }
    elseif ($command -match 'sys.version_info\\[0\\]') {
        Write-Output '3'
    }
    exit 0
}

if ($Args.Count -ge 3 -and $Args[0] -eq '-m' -and $Args[1] -eq 'PyInstaller' -and $Args[2] -eq '--version') {
    Write-Output '6.19.0'
    exit 0
}

if ($Args.Count -ge 3 -and $Args[0] -eq '-m' -and $Args[1] -eq 'venv') {
    $venvRoot = $Args[$Args.Count - 1]
    $scripts = Join-Path $venvRoot 'Scripts'
    New-Item -ItemType Directory -Force -Path $scripts | Out-Null
    $wrapper = "@echo off`r`npowershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`" %*`r`n"
    Set-Content -LiteralPath (Join-Path $scripts 'python.cmd') -Value $wrapper -Encoding ASCII
    exit 0
}

if ($Args.Count -ge 2 -and $Args[0] -eq '-m' -and $Args[1] -eq 'pip') {
    exit 0
}

if ($Args.Count -ge 2 -and $Args[0] -eq '-m' -and $Args[1] -eq 'PyInstaller') {
    exit 0
}

    exit 0
""".strip()
        + "\n",
        encoding="utf-8",
    )

    env = dict(os.environ)
    env["PATH"] = str(tools_dir) + os.pathsep + env.get("PATH", "")

    result = subprocess.run(
        [
            "powershell",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script_root / "build_backend_onefile.ps1"),
            "-Python",
            str(fake_python),
        ],
        cwd=script_root,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0, result.stdout + result.stderr
    assert "Expected staged backend bundle was not produced" in (result.stdout + result.stderr)
    assert not (script_root / "last_build_path.txt").exists()


def test_build_backend_onefile_includes_passlib_bcrypt_hidden_import():
    root = Path(__file__).resolve().parents[2]
    backend_script = (root / "build_backend_onefile.ps1").read_text(encoding="utf-8")
    dependency_lock = (root / "requirements.lock").read_text(encoding="utf-8")

    assert "passlib.handlers.bcrypt" in backend_script
    assert "python-multipart==" in dependency_lock
    assert "--onedir" in backend_script
    assert "--onefile" not in backend_script
    assert "--console" in backend_script
    assert "--noconsole" not in backend_script
    assert 'Remove-PathWithRetry -Path ".\\\\build"' not in backend_script
    assert '$pyInstallerDistRoot = ".\\build\\$BackendBuildSlug-backend-publish"' in backend_script
    assert '$pyInstallerWorkRoot = ".\\build\\$BackendBuildSlug-pyinstaller-work"' in backend_script
    assert backend_script.index("$allowedProcessRoots") < backend_script.index(
        "$distDir = $BackendDistRelative"
    )
    assert '$stagedToolsBin = Join-Path $stagedOut "tools\\bin"' in backend_script
    assert 'Join-Path $stagedToolsBin "ffmpeg.exe"' in backend_script
    assert 'Join-Path $stagedToolsBin "ffplay.exe"' in backend_script
    assert 'Join-Path $stagedToolsBin "ffprobe.exe"' in backend_script
    assert '.\\app\\services\\qwen_tts_cli.py;app\\services' in backend_script
    assert '.\\app\\services\\qwen_tts_server.py;app\\services' in backend_script
    assert '.\\app\\services\\omnivoice_cli.py;app\\services' in backend_script
    assert "backend-rtai" not in backend_script
    assert "radiotedu" in backend_script


def test_ensure_dotnet_reinstalls_when_existing_sdk_version_mismatches(tmp_path):
    root = Path(__file__).resolve().parents[2]
    script_root = tmp_path / "dotnet"
    scripts_dir = script_root / "scripts"
    dotnet_dir = script_root / ".dotnet"
    scripts_dir.mkdir(parents=True)
    dotnet_dir.mkdir(parents=True)

    shutil.copy2(root / "scripts" / "ensure_dotnet.ps1", scripts_dir / "ensure_dotnet.ps1")

    installer_script = scripts_dir / "dotnet-install.ps1"
    installer_script.write_text(
        """
param(
    [string]$Version,
    [string]$InstallDir,
    [string]$Architecture
)

Set-Content -Path (Join-Path $InstallDir 'version.txt') -Value $Version -NoNewline
Set-Content -Path (Join-Path $InstallDir 'installed.txt') -Value $Architecture -NoNewline
""".strip()
        + "\n",
        encoding="utf-8",
    )

    fake_dotnet_source = script_root / "fake-dotnet"
    fake_dotnet_source.mkdir()
    (fake_dotnet_source / "fake-dotnet.csproj").write_text(
        """
<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup>
    <OutputType>Exe</OutputType>
    <TargetFramework>net8.0</TargetFramework>
    <AssemblyName>dotnet</AssemblyName>
    <ImplicitUsings>enable</ImplicitUsings>
    <Nullable>enable</Nullable>
  </PropertyGroup>
</Project>
""".strip()
        + "\n",
        encoding="utf-8",
    )
    (fake_dotnet_source / "Program.cs").write_text(
        """
using System;
using System.IO;

var versionFile = Path.Combine(AppContext.BaseDirectory, "version.txt");
var version = File.Exists(versionFile) ? File.ReadAllText(versionFile).Trim() : "missing";

if (args.Length > 0 && args[0] == "--version")
{
    Console.WriteLine(version);
    return 0;
}

Console.WriteLine(version);
return 0;
""".strip()
        + "\n",
        encoding="utf-8",
    )

    dotnet_exe = root / ".dotnet" / "dotnet.exe"
    dotnet_cmd = str(dotnet_exe) if dotnet_exe.exists() else (shutil.which("dotnet") or "")
    if not dotnet_cmd:
        pytest.skip("dotnet SDK is not available")

    publish_result = subprocess.run(
        [
            dotnet_cmd,
            "publish",
            str(fake_dotnet_source / "fake-dotnet.csproj"),
            "-c",
            "Release",
            "-r",
            "win-x64",
            "--self-contained",
            "true",
            "-o",
            str(dotnet_dir),
        ],
        cwd=script_root,
        capture_output=True,
        text=True,
    )
    assert publish_result.returncode == 0, publish_result.stdout + publish_result.stderr

    (dotnet_dir / "version.txt").write_text("0.0.0", encoding="utf-8")

    result = subprocess.run(
        [
            "powershell",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(scripts_dir / "ensure_dotnet.ps1"),
            "-InstallDir",
            str(dotnet_dir),
            "-Version",
            "8.0.415",
        ],
        cwd=script_root,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert (dotnet_dir / "installed.txt").read_text(encoding="utf-8") == "x64"
    assert (dotnet_dir / "version.txt").read_text(encoding="utf-8") == "8.0.415"
