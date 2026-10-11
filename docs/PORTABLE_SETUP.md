# Windows, Linux and macOS setup

The existing Windows desktop installer remains available. Linux and macOS use
the same browser-based On Air panel and independent station workers. The
Windows WPF shell and Windows service installer are Windows-only.

## Source setup

Use Python **3.12** and FFmpeg with the encoder required by your saved station
configuration. Install FFmpeg using your OS package manager, or select explicit
binary paths at launch. AAC/HE-AAC configurations that require `libfdk_aac`
need an appropriate operator-supplied FFmpeg build. The launcher checks for
FFmpeg and ffprobe; verify the selected encoder during commissioning. Existing
AAC-LC compatibility recovery can use native AAC at the same bitrate if FDK is
missing. Use the required encoder build to preserve the intended encoding quality.

```sh
python3.12 installer/install_onair.py --check
python3.12 installer/install_onair.py --prefix "$HOME/RadioTEDU-OnAir"
```

On Windows use `py -3.12` instead of `python3.12`. The installer prints a versioned
launcher. Every installation has a separate version directory; it does not stop
a service, overwrite an existing application, or restore a database.

The launcher opens `http://127.0.0.1:18110/`. An occupied port is rejected. Use
`--port 18210 --data-root /path/to/separate-test-state` for an isolated instance.
To run directly from the checkout after installing `requirements.lock`:

```sh
python3.12 onair_portable.py --check
python3.12 onair_portable.py --ffmpeg /path/to/ffmpeg --ffprobe /path/to/ffprobe
```

GitHub Actions builds a Windows x64, Linux x64 and macOS ARM64 portable directory
on a tag or manual workflow run. Download the artifact, extract it, install the
required FFmpeg tools, and launch `RadioTEDU-OnAir`. These OS builds must pass
their actual runners before being described as verified release packages.

## State and credentials

- Windows portable state: `%LOCALAPPDATA%/RadioTEDU/OnAir`; existing installed
  service configuration continues to use its explicit ProgramData paths.
- Linux portable state: `$XDG_DATA_HOME/radiotedu/onair`, normally
  `~/.local/share/radiotedu/onair`.
- POSIX product media defaults to the instance data directory's `media` folder;
  set `RADIOTEDU_MEDIA_ROOT` to an absolute operator-selected media directory.
- macOS portable state: `~/Library/Application Support/RadioTEDU/OnAir`.
- `CLEANROOM_DATA_ROOT`, `CLEANROOM_DB_PATH` and `CLEANROOM_USER_CONFIG_ROOT`
  remain explicit overrides. Back up the database using SQLite online backup.
- Windows retains DPAPI credential protection. The portable POSIX launcher
  explicitly selects Fernet encryption with an owner-only local key. Keep
  `config/secrets/credential-encryption.key` with a separate protected backup;
  an encrypted credential vault is unusable without its original key.
- Move a database to a different OS only in an isolated commissioning instance.
  Re-enter source credentials, choose valid local media folders, and verify the
  commissioned outputs before it can replace an existing broadcaster.

## Microphone and transitions

In **On Air**, click **Mikrofonları algıla**, grant permission, and choose an input.
The selected hardware is used explicitly. **Canlı mikrofonu aç** starts capture;
the same button stops it. The music scheduler and output connections continue.
Use headphones. The microphone supplies mono 48 kHz PCM through an AudioWorklet;
browser echo cancellation, noise suppression and automatic gain are disabled.

Choose automatic ducking, a continuous music bed, or microphone-only mode.
Gain and bed settings are persisted and read back. Crossfade is configured per
station, applies on the next music transition, and never rewrites outputs.
An interrupted native microphone stops owning the live input after three
seconds. Music returns when signal reception stops. Other live/emergency
sources must be stopped before microphone capture can begin.

Live music folders in **replace** mode are authoritative. New stable files are
appended once to the pending queue and enter rotation; removed files leave future queue entries, while the currently playing
item finishes. Incomplete/unreadable scans retain the last verified inventory.
Retired files under `.radiotedu-trash` are not imported again.

## Rollout and rollback

Keep the prior source bundle and a verified SQLite online backup. Commission on
an isolated data root and local sink first. Do not run a second production source
for an occupied mount. Retain the original output roster, codecs, bitrates and
credentials. A code rollout that requires replacing workers needs a documented
handoff and decoded-listener verification; copying code alone does not prove
that already-imported worker code changed.

Protocol references: [FFmpeg codec documentation](https://www.ffmpeg.org/ffmpeg-codecs.html),
[SQLite online backup API](https://docs.python.org/3/library/sqlite3.html#sqlite3.Connection.backup).
