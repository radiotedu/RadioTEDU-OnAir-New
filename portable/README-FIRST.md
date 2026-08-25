# RadioTEDU OnAir portable recovery

This private backup contains the application source, a consistent SQLite
snapshot, the configured stream mounts, and every stream credential needed by
the snapshot. The outer RAR and the inner credential payload are encrypted.

## macOS recovery

1. Extract the RAR with Keka, The Unarchiver, or the official RAR utility.
2. Use the archive password supplied by the RadioTEDU operator.
3. Double-click `INSTALL-AND-START-RADIOTEDU-MAC.command`.
4. If Finder removed the executable bit, open Terminal in this folder and run:

   `bash INSTALL-AND-START-RADIOTEDU-MAC.command`

The first run installs/checks Homebrew, Python 3.12, and a non-distributed local
FFmpeg build with `libfdk_aac`; it then asks for the mounted media-disk root.
Passwords are imported into macOS Keychain and are not written to the launchd
configuration. A per-user LaunchAgent starts RadioTEDU after login and restarts
it after unexpected exits. Subsequent manual starts use
`START-RADIOTEDU-MAC.command`.

The audio library itself is not included. Attach the RadioTEDU media disk and
select its mount under `/Volumes` when asked. Paths that originally began with
`H:\` are translated in the restored database.

## Windows recovery

Double-click `INSTALL-AND-START-RADIOTEDU-WINDOWS.bat`. The bundle includes the
private Windows FFmpeg tools used by this installation when they were available
at backup time. The importer re-protects credentials with Windows DPAPI.

## Security boundary

`stream-mounts.json` lists mounts and codec settings but no passwords. Passwords
are inside `private/portable-secrets.bin`, encrypted with AES-256-GCM using a
PBKDF2-derived key. Never upload this RAR or its extracted `private` directory to
GitHub, cloud storage, or a public web server.

## Scope and expectations

The bundle is designed for fast recovery, not simultaneous duplicate playout.
Do not start it while another machine is already connected to the same Icecast
mounts. Server reachability, the mounted media library, and a valid local
FFmpeg/libfdk build are still required.
