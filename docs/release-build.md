# RadioTEDU OnAir Release Build

## Build

1. Run `powershell -ExecutionPolicy Bypass -File .\installer\build_setup.ps1 -Version <version>`.
2. Run `powershell -ExecutionPolicy Bypass -File .\smoke_test_desktop_bundle.ps1`.
3. Review `release\setup\last_setup_path.txt` and `last_build_path.txt`.

If Inno Setup is installed outside `PATH`, pass `-InnoSetupCompiler <path-to-ISCC.exe>` or set `INNO_SETUP_COMPILER`.

The installer authoring files are open source under `installer/LICENSE.md`; release payloads remain under their own licenses.

Official builds require the self-contained .NET desktop publish to succeed. In
the installer, the .NET 8 Desktop Runtime and local Ollama runtime are separate,
unchecked optional tasks. WebView2 is verified and bootstrapped only when it is
missing.

## Expected Artifacts

- `release\setup\RadioTEDU-OnAir-Setup-<version>.exe`

Before public distribution, sign the setup executable with RadioTEDU
Technologies' Authenticode certificate and publish a SHA-256 checksum alongside
it. Local development builds are expected to report `NotSigned`; do not
describe an unsigned build as production-signed.
- `dist\backend\RadioTEDU-OnAir-Backend.exe`
- `dist\desktop\RadioTEDU-OnAir-Agent.exe`
- `dist\desktop\shell\RadioTEDU-OnAir.exe`

## Operator Acceptance

- Install completes without manual command-line work.
- Desktop shortcut is created by default.
- App opens as a desktop window.
- First-run setup accepts the operator's Icecast server URL, mount, source username, source password, and codec choice without command-line work.
- First-run setup blocks completion until local monitor, a real short Icecast test stream, Ollama, local Qwen TTS, and AI startup-buffer checks are verified.
- Completion issues a RadioTEDU deployment certificate for the exact verified station/output/AI configuration.
- Restarting the app resumes partial setup instead of silently entering an incomplete state.
