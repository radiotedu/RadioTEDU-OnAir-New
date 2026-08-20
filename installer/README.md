# RadioTEDU OnAir Open-Source Installer

The RadioTEDU OnAir installer authoring files are open source under
the MIT license in `installer/LICENSE.md`.

This directory contains the complete installer source:

- `RadioTEDUBroadcastRoomSetup.iss`: Inno Setup installer definition.
- `build_setup.ps1`: reproducible Windows installer build entrypoint.
- `EnsureDesktopPrerequisites.ps1`: prerequisite bootstrapper for WebView2,
  .NET Desktop Runtime, and Ollama.
- `generate_brand_assets.ps1`: deterministic wizard artwork generator.
- `assets/`: generated installer wizard bitmap assets.

## Build From Source

Install Inno Setup 6 and make `ISCC.exe` available one of three ways:

- Add `ISCC.exe` to `PATH`.
- Set `INNO_SETUP_COMPILER` to the full `ISCC.exe` path.
- Pass `-InnoSetupCompiler C:\Path\To\ISCC.exe`.

Then run:

```powershell
powershell -ExecutionPolicy Bypass -File .\installer\build_setup.ps1
```

For an existing backend bundle:

```powershell
powershell -ExecutionPolicy Bypass -File .\installer\build_setup.ps1 -SkipBackendBuild
```

Expected output:

```text
release\setup\RadioTEDU-OnAir-Setup-1.0.2.exe
release\setup\last_setup_path.txt
```

## Scope

The installer source is open and rebuildable. The generated installer may
bundle RadioTEDU OnAir application binaries, optional AI model assets,
Microsoft runtimes, Ollama, and other third-party components. Those payloads
remain under their own licenses and redistribution terms.

The RadioTEDU name, publisher identity, and visual brand are not granted as
trademark rights by the installer source license.
