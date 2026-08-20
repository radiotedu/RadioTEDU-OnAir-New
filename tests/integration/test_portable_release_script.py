import subprocess
from pathlib import Path


def test_package_portable_release_creates_single_exe_release(tmp_path):
    backend_root = Path(__file__).resolve().parents[2]
    script = backend_root / "package_portable_release.ps1"
    fake_exe = tmp_path / "dist" / "RadioTEDU-OnAir-Backend.exe"
    release_root = tmp_path / "release"
    last_release_path_file = tmp_path / "last_release_path.txt"

    fake_exe.parent.mkdir(parents=True, exist_ok=True)
    fake_exe.write_bytes(b"portable-release-test")

    result = subprocess.run(
        [
            "powershell",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
            "-ExePath",
            str(fake_exe),
            "-ReleaseRoot",
            str(release_root),
            "-LastReleasePathFile",
            str(last_release_path_file),
        ],
        cwd=backend_root,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr

    release_dirs = list(release_root.glob("radiotedu-broadcast-room-portable-*"))
    assert len(release_dirs) == 1

    release_dir = release_dirs[0]
    copied_exe = release_dir / "RadioTEDU-OnAir-Backend.exe"
    assert copied_exe.read_bytes() == b"portable-release-test"
    assert [item.name for item in release_dir.iterdir()] == ["RadioTEDU-OnAir-Backend.exe"]
    assert last_release_path_file.read_text(encoding="utf-8").strip() == str(
        copied_exe.resolve()
    )
