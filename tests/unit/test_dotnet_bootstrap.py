from pathlib import Path


def test_desktop_bundle_delegates_to_local_dotnet_bootstrap_script():
    root = Path(__file__).resolve().parents[2]
    bundle_script = (root / "build_desktop_bundle.ps1").read_text(encoding="utf-8")

    assert "ensure_dotnet.ps1" in bundle_script
    assert "scripts\\ensure_dotnet.ps1" in bundle_script


def test_ensure_dotnet_bootstrap_uses_repo_local_installer_and_dotnet_root():
    root = Path(__file__).resolve().parents[2]
    ensure_script = (root / "scripts" / "ensure_dotnet.ps1").read_text(
        encoding="utf-8"
    )

    assert "https://dot.net/v1/dotnet-install.ps1" in ensure_script
    assert "Invoke-WebRequest" in ensure_script
    assert "dotnet-install.ps1" in ensure_script
    assert ".dotnet" in ensure_script
