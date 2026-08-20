# RadioTEDU normal-stream autonomy and codecs

Updated: 2026-08-13

## Production streams on this broadcast PC

| Station | Mount | Encoder profile | Autostart |
|---|---|---|---|
| Classical | `/classic` | Opus, 96 kbps | Enabled |
| Lo-Fi | `/lofi` | Opus, 96 kbps | Enabled |
| Pop / Radio | `/radio` | Opus, 96 kbps | Enabled |
| Jazz | `/cazz` | Opus, 96 kbps | Enabled |
| Rock | `/rock` | Opus, 96 kbps | Enabled |
| Energize | `/energize` | Opus, 96 kbps | Enabled |

There is no `/radiotedu` mount. The stale station-7 output placeholder is
disabled, has an empty mount, and is not an autostart station. `/en` and `/fr`
are external AI radios and are not sourced by this broadcast PC.

Source files and stream codecs are different things. A FLAC song in a station
library preserves the lossless library copy, but the normal live output is
encoded with the station profile in the table above.

## Active quality-output contract

Every music station has four quality-suffixed outputs. The broadcast PC keeps
all 14 local source branches active and independently reconnecting:

| Suffix | Defined codec | Current state |
|---|---|---|
| `-low` | Opus, 64 kbps | Enabled |
| `-normal` | Opus, 96 kbps | Enabled |
| `-high` | Opus, 192 kbps | Enabled |
| `-flac` | Ogg/FLAC lossless | Enabled |

The origin must provision at least 32 concurrent sources (40 recommended).
Origin rejection degrades only the affected delivery branches; it does not
stop decoded playout or trigger destructive whole-station restart loops.

## Autonomous recovery

`RadioTEDU.OnAir.Supervisor` is the machine-owned service. In its normal state:

- Windows starts it automatically after boot with delayed automatic startup.
- It depends on TCP/IP and DNS so boot does not race basic networking.
- Windows restarts service failures after 5, 15, and 60 seconds.
- The six station-level `broadcast_autostart_enabled` settings restore normal
  playout without an operator session.
- Each Icecast source connector retries indefinitely with bounded backoff of
  1, 2, 4, 8, 15, then 30 seconds while the programme timeline stays alive.
- A short bounded PCM queue bridges transient source-server interruptions.

The one-time 2026-08-13 cooldown task is SYSTEM-owned, uses **Start when
available**, and restores delayed automatic startup before starting the service.
This means a power interruption during the cooldown does not lose the restart.

## Operator UI workflow

Use the OnAir UI for normal operation:

1. Select a station.
2. Use **Broadcast Start/Stop** for that station; do not stop the Windows
   supervisor to silence a single station.
3. Use **Streaming** to edit host, port, mount, protocol, codec profile, and
   bitrate. Save and apply, then require the UI read-back confirmation.
4. Use the station **Start automatically** setting to control whether that
   station resumes after service or PC restart.
5. Leave all four quality-output switches off until the origin has been
   replaced or capacity-tested outside production.

The UI writes the same persisted station output and station setting records the
runtime reads at startup. No source credential is displayed or stored in the
quality-output settings.

## Verification boundary

Autonomous local restart and network reconnection cannot repair a remotely hung
TinyIce process. If all normal listeners remain unresponsive after local sources
have reconnected, restart TinyIce on its server; OnAir will reconnect without
further local configuration.
