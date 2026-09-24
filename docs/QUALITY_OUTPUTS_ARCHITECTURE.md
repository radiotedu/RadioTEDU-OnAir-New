# RadioTEDU quality outputs architecture and release gates

Status: source implemented and targeted tests passing; live quality outputs remain fail-closed until the TinyIce origin is recovered and canary-tested.

## Invariants

- The eight existing mounts—`/classic`, `/lofi`, `/cazz`, `/energize`, `/radio`, `/rock`, `/en`, `/fr`—remain unchanged outputs. They are never renamed, redirected, aliased, or re-encoded.
- The TinyIce credential inventory provisions 24 quality mounts for the six music stations. It does not provision `/en-*` or `/fr-*`; AI therefore remains legacy-only.
- Each music station has one authoritative program timeline. Four encoders receive the same decoded PCM and never select tracks independently.
- Quality branches inherit the protected legacy source credential in memory. Credentials are absent from quality settings, API/browser payloads, logs, diagnostics, and backups.
- Public title/artist metadata is suppressed. One completed item creates one compliance record with delivered variants, not four broadcasts.

## Canonical outputs

| Station | Additional mounts |
|---|---|
| Classical | `/classic-low`, `/classic-normal`, `/classic-high`, `/classic-flac` |
| Lo-Fi | `/lofi-low`, `/lofi-normal`, `/lofi-high`, `/lofi-flac` |
| Jazz | `/cazz-low`, `/cazz-normal`, `/cazz-high`, `/cazz-flac` |
| Energize | `/energize-low`, `/energize-normal`, `/energize-high`, `/energize-flac` |
| Pop / Radio | `/radio-low`, `/radio-normal`, `/radio-high`, `/radio-flac` |
| Rock | `/rock-low`, `/rock-normal`, `/rock-high`, `/rock-flac` |

| Suffix | Encoding target | Use |
|---|---|---|
| `-low` | Opus, 64 kbps | constrained connections |
| `-normal` | Opus, 96 kbps | recommended/default |
| `-high` | Opus, 192 kbps | high-quality lossy |
| `-flac` | FLAC in Ogg | lossless |

Opus is the only lossy output family exposed by the operator UI. The managed
FFmpeg must provide `libopus`; MP3 and AAC remain read-compatible historical
values only and must not appear as presets, defaults, or rollback targets.

## Runtime and failure containment

The station decodes the current program item once and fans identical PCM chunks to the preserved legacy sink and independent quality sinks. Each quality sink owns an encoder, bounded queue, source connection, health, and reconnect state. A blocked or failed branch cannot block siblings or the legacy output. When a branch falls behind, stale PCM is discarded before reconnect so it returns to the live timeline instead of playing delayed audio.

The legacy sink retains its existing mount, codec, bitrate, public policy, host, username, and protected password. Quality branches forcibly clear title/artist metadata. Compliance attaches delivered quality names to the single finished-play record with `publication_count=1`.

The operator panel exposes enable/public controls and immutable canonical mount/codec/bitrate values for the six provisioned stations. Save performs read-back verification. Diagnostics checks mappings, credentials, native AAC support, runtime branches, and verified origin capacity. Apply refreshes only music runtimes; it does not restart `/en` or `/fr`.

## Threat/failure analysis

| Failure | Containment / acceptance evidence |
|---|---|
| Encoder or source blocks | bounded non-blocking branch queue; siblings and legacy continue during fault injection |
| TinyIce globally hangs | watchdog confirms origin twice and suppresses all healthy local/AI restart churn; remote origin must be restarted |
| Legacy source fails | report unhealthy and reconnect it; never repoint listeners or mutate mount semantics |
| Branch drifts | drop stale backlog, reconnect at live edge, compare transitions/fingerprints during canary |
| Duplicate compliance royalties | one idempotent play event with delivered variants and `publication_count=1` |
| Metadata leak | no stream title/artist on quality branches; probe every public mount |
| Secret leak | no credentials in settings, API, UI, logs, bridge, backup, or test output |
| Origin rejects load | capacity begins at unknown (`0`), never an assumed value; prove at least 32 total slots and prefer 40 |
| CPU/uplink exhaustion | stage one station low/normal, then high/FLAC, then expand station-by-station |
| FLAC backpressure | isolate branch, retain live legacy/AAC paths, require sustained listener bytes before promotion |
| Mobile endpoint fails | phone/player fallback chain: selected → normal → low → high → unchanged legacy; FLAC is never automatic |

## Phased live acceptance

1. Restart TinyIce at `<tinyice-lan-host>` and require two responsive root/status checks with zero source churn.
2. Start all eight legacy sources and record continuous decode/audibility plus baseline codec/metadata evidence.
3. Verify at least 32 source slots (40 recommended); store evidence rather than a desired number.
4. Canary Lo-Fi low/normal. Verify exact AAC-LC rate, metadata suppression, synchronization, stable queues, listener bytes, CPU, memory, and uplink.
5. Add Lo-Fi high/FLAC; inject independent failures and prove legacy/siblings continue.
6. Roll out the remaining five music stations one at a time. Stop immediately on any legacy regression, silence, drift, queue pressure, or source-limit rejection.
7. Run two 600-second watchdog cycles and an unattended audible/fault soak across eight legacy plus 24 quality sources.
8. Package version 1.0.2 only after zero unexplained silence/disconnects and complete acceptance evidence.

Rollback disables only suffixed outputs and applies the saved state. It does not delete settings, backups, compliance events, media, or legacy mounts.

## Primary documentation

- TinyIce formats and Icecast-compatible source API: <https://github.com/Eartharoid/TinyIce>
- FFmpeg AAC encoder and Icecast protocol: <https://ffmpeg.org/ffmpeg-codecs.html#aac>, <https://ffmpeg.org/ffmpeg-protocols.html#Icecast>
- FFmpeg ADTS/Ogg/FLAC formats: <https://ffmpeg.org/ffmpeg-formats.html>
- Nginx proxy buffering: <https://nginx.org/en/docs/http/ngx_http_proxy_module.html>
