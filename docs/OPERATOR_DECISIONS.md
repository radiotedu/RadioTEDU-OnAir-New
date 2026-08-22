# RadioTEDU operator decisions

This file records durable operator instructions that must survive future work.

## Pop jingle policy — 2026-08-22

- Scope is **RadioTEDU Pop only**, station ID `4`.
- The rule applies to `/radio` and its `/radio-low` quality output.
- The only active Pop jingles are:
  - `H:\RadioTEDU Songs\Ads\Jingles\Pop\TEDU_1.mp3`
  - `H:\RadioTEDU Songs\Ads\Jingles\Pop\TEDU_2.mp3`
  - `H:\RadioTEDU Songs\Ads\Jingles\Pop\TEDU_3.mp3`
  - `H:\RadioTEDU Songs\Ads\Jingles\Pop\TEDU_4.mp3`
  - `H:\RadioTEDU Songs\Ads\Jingles\Pop\TEDU_5.mp3`
  - `H:\RadioTEDU Songs\Ads\Jingles\Pop\TEDU_6.mp3`
- Play one Pop jingle after every three music tracks.
- Keep the Pop sweeper enabled, with interval `3`, unit `tracks`, and ordered/deterministic selection.
- Do not generate or activate other jingles for Pop while this decision is current.
- Do not apply this policy to Classical, Lo-Fi, Jazz, Rock, Energize, or any other station.

Recovery snapshot:

`C:\Users\tedu\Desktop\RadioTEDU Safety Backups\20260822-225711-Pop-Jingle-Change`

## Cache, sound and rights-report policy — 2026-08-22

- Cached H:/library media is disposable: play from the local read-ahead copy,
  then release that copy after its final FFmpeg reader. Never delete or change
  the source-library file.
- Keep the disposable cache at or below 4 GiB. The independent maintenance
  task `RadioTEDU-OnAir-FastAudioCache-Prune` runs every five minutes, protects
  the newest 15 minutes, ignores overlapping runs and retries failures.
- Use the conservative `balanced` broadcast processing profile by default:
  30 Hz high-pass, gentle compression, configured loudness normalization,
  -1.5 dBTP safety limiting, then final 48 kHz resampling. Keep wider-dynamic
  `transparent` as the intended option for Classical and Jazz.
- Music-to-music handoffs use the existing three-second equal-power (`qsin`)
  crossfade. Jingle transitions retain their short safety cap.
- Keep the immutable, hash-chained complete playout history, including station
  imaging, for audit/recovery. Licensor CSVs and MESAM-shaped station forms
  must include only tracks explicitly classified as `music`.
- Rights reports include station/mount, title, performer, version, writers,
  phonogram producer, label, ISRC, scheduled duration, completed play count,
  event count, total played seconds, source path and first/last UTC air times.
- Refresh Desktop and H: reports every five minutes and back the Desktop tree
  up to the configured private Git history repository nightly at 00:15.
- Do not submit, email, upload or otherwise send reports to MESAM, MÜYAP or any
  other licensor. Files are for RadioTEDU records; only the operator-requested
  Git backup is an external destination.

Recovery snapshots:

- `C:\ProgramData\RadioTEDU\OnAir\backups\play-history-reporting-20260822T233231`
- `C:\ProgramData\RadioTEDU\OnAir\backups\audio-source-staging-20260822T234239`

## Icecast AAC listener policy — 2026-08-23

- Every suffix-free normal music mount uses libfdk_aac AAC-LC (`aac_low`) at
  192 kbps. This includes `/classic`, `/lofi`, `/radio`, `/cazz`, `/rock`, and
  `/energize`.
- Every `-low` music mount uses libfdk_aac HE-AAC v2 (`aac_he_v2`) at 64 kbps.
- Lossless `/classic-flac` and `/cazz-flac` are not changed and do not receive
  lossy bitrate source headers.
- Icecast source handshakes advertise `Ice-Bitrate` and `Ice-Audio-Info` for
  lossy AAC so listener metadata (`icy-br`) is 192 or 64 instead of 320.
- If a new AAC profile cannot deliver its first encoded audio, the sink falls
  back to the already-proven current profile: HE-AAC v1 192 for normal and
  HE-AAC v1 96 for low. The requested and effective profiles remain visible in
  runtime health diagnostics.
- Do not disconnect an existing public source merely to activate this policy.
  Apply the staged migration during an already-required backend start; the
  idempotent migration changes only canonical normal and low rows and leaves
  FLAC untouched.
- The real Icecast origin passed private, non-public canaries for AAC-LC 192
  and HE-AAC v2 64 on 2026-08-23. Both canary mounts were closed and removed.

Recovery snapshot:

`C:\ProgramData\RadioTEDU\OnAir\backups\aac-policy-20260823T002626`
