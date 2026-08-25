# Portable recovery architecture

The public repository contains no stream passwords. A private recovery bundle
is produced by `tools/create_portable_recovery_bundle.py` and then placed in a
RAR archive with encrypted headers.

The bundle contains a SQLite online-backup snapshot, a public mount inventory,
and an AES-256-GCM credential payload. The payload key is derived with PBKDF2
SHA-256 (600,000 iterations). On restore, credentials are re-protected with
Windows DPAPI or a random AES key held by macOS Keychain.

macOS uses a per-user LaunchAgent, a Homebrew Python 3.12 environment, and a
local, non-distributed FFmpeg build that exposes `libfdk_aac`. The installer
validates that encoder before restoring or starting the streams. The restored
database can translate `H:\` media paths to a selected `/Volumes/...` root.

The media library is intentionally not copied into the recovery archive. It
must be mounted separately. Never run two recovered hosts against the same
Icecast mounts at the same time.
