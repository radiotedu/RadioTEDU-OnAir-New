# Installer Third-Party Notices

## Inno Setup

RadioTEDU OnAir uses Inno Setup 6 as the installer authoring and
compiler toolchain.

- Project: `jrsoftware/issrc`
- Source: `https://github.com/jrsoftware/issrc`
- License: Inno Setup License, a permissive open-source license. Preserve the
  upstream copyright and license notices when redistributing Inno Setup itself
  or modified Inno Setup source/binaries.

The RadioTEDU installer source in this repository does not vendor Inno Setup
source code. It expects a separately installed `ISCC.exe` compiler.

## Bootstrap Payloads

The installer can download or include optional prerequisite payloads:

- Microsoft Edge WebView2 Runtime
- Microsoft .NET Desktop Runtime
- Ollama
- Private Python runtime and local Qwen TTS assets when packaged for offline
  provisioning

These payloads are not licensed by `installer/LICENSE.md`. They must be
distributed only according to their respective upstream licenses and terms.
