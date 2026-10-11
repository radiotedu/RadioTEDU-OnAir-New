# RadioTEDU OnAir 1.1.0

The On Air panel now prepares and enumerates browser microphone inputs, selects
the exact physical device, and starts/stops a live microphone with one control.
Mono 48 kHz signed PCM reaches the existing worker bridge without a microphone
decoder. Automatic browser gain/noise processing is disabled. An AudioWorklet
sends 100 ms packets; bounded queues fail back to music instead of accumulating
unbounded latency. The panel confirms PCM receipt before displaying CANLI.

The existing station encoder remains connected during microphone use. The mixer
preserves the stereo music image, smooths music gain, holds ducking between words
and uses linked sample-peak limiting. This is sample-peak protection, not a claim
of true-peak or EBU compliance. The existing encoder/loudness chain is retained.
Elementwise DSP runs in NumPy's native array code; bounded envelope blocks avoid
long Python sample loops and preserve limiter state across network packets.
Microphone-only mode returns to music when input reception stalls; expired native
sessions release ownership after three seconds. Closing or switching stations
stops capture and restores the original persisted mix, with read-back checks.

Crossfade duration is persisted per station (0–10 seconds) and applied at the
next music transition. Existing prewarm, buffered-tail, advertisement ownership,
source acceptance and reconnection safeguards remain in place. No codec or output
configuration is rewritten by changing the transition duration.
The historical Ogg-to-AAC startup migration is now explicitly opt-in through
`CLEANROOM_ENABLE_LEGACY_CODEC_MIGRATION`; normal startup preserves saved codecs.

Managed-folder scans now reject incomplete inventories and exclude
`.radiotedu-trash`. Existing replace-mode reconciliation removes deleted songs
from future queue entries while allowing the current item to finish; new stable
files enter rotation. An unavailable drive cannot empty the managed inventory.
Watcher imports append fresh or reactivated music files once, after existing
operator queue entries, in the same transaction as the inventory update.
POSIX inventory keys preserve filename case so Linux `Track.mp3` and `track.mp3`
remain independent media entries.

The browser panel has Windows, Linux and macOS launchers and an isolated source
installer. GitHub builds portable directories on real OS runners. FFmpeg remains
operator supplied; missing configured encoders do not trigger quality fallback.
Windows uses DPAPI; the explicit POSIX launcher uses a protected Fernet key.
See [setup and key backup](PORTABLE_SETUP.md).

## Commissioning and rollback

Before updating a broadcast PC, retain a SHA256 source manifest and a verified
SQLite online backup. Test this release in a separate directory and data root.
Read the actual persisted station/output roster: historical mount/codec examples
in older runbooks must never synthesize or disable outputs during an upgrade.

The existing supervised backend reload calls stop_all; rolling worker reloads
also replace a source process. Neither is a zero-interruption hot upgrade.
Replacing worker code requires a controlled maintenance handoff and independent
decoded-listener verification. Copying Python files cannot activate already
imported modules. Never start a competing source on an occupied production mount.

Rollback restores the previous source version using the same controlled handoff.
There is no schema migration in this release. Preserve the current operational
database and queues; restore a historical database only for actual data damage.
Do not overwrite operator media or current credentials during code rollback.

Regression tests from the current deployed engine commit `31d39ff` are retained.
An older operator checkout contained stale fake-producer fixtures; those were
restored from that engine commit before testing the feature. The original source
snapshot remains separately backed up and in commit `4d64fcf`.
