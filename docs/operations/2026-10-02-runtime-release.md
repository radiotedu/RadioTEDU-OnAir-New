# Runtime release, 2 October 2026

## Scope

The installed application had not received the complete source EOF, durable
advertising, and output FIFO fixes already present in the Git checkout. Apply
the reviewed application bundle together, then restart the Windows Supervisor
once. The excluded legacy API file retains the installed media-import defaults.

Preserve all eight stations and all 16 enabled outputs. Primaries use AAC LC
192 kbps; six low variants use HE-AACv2 64 kbps; the Classic and Jazz FLAC
outputs remain lossless. Main Character runs under the common Supervisor;
the obsolete separate stream keeper remains disabled.

## Changes

- Primary output backpressure is isolated from the common PCM producer and
  sibling encoders using a bounded dispatch FIFO.
- Output recovery retains queued programme audio and can restart a failed
  dispatcher without replacing a healthy encoder.
- An advertisement retains playout ownership until source EOF, output
  acceptance, and drain evidence agree. Failures retain a durable retry state.
- A healthy pending media queue is checked with a read query; it does not
  acquire a write transaction on every scheduler tick.
- Database bootstrap evidence is invalidated by database/WAL changes. Repeated
  checks of an unchanged database reuse the verified result. Keep one cache
  signature per database and mode.
- An inactive managed campaign does not report a commissioned profile drift.
- Legacy implicit localhost defaults recognize the commissioned AAC preset;
  configured production output rows are not changed.
- Health reporting separates active programme, source delivery, and verified
  remote audio. It must not declare unavailable evidence healthy.
- The post-deployment canary exposed listener-driven reconnects. Source sinks
  do not perform recurring listener GETs. The Supervisor and worker liveness
  use explicit `source_health` with current encoder/writer evidence; the
  independent decoded verifier owns public delivery evidence. Missing listener
  verification must neither certify delivery nor reconnect a healthy writer.
- Queue output readiness and output recovery also consume `source_health`.
  The loopback Health Wall displays the playing title/artist when the real
  programme is rendering fresh PCM, even when public delivery is unverified.
  Its public delivery status remains independent.
- Fast local health snapshots skip synchronous origin listener requests. A
  failed origin must not queue eight network timeouts inside each desktop
  monitor refresh. Public endpoint probing remains available separately.

## Backup and deployment

The online SQLite backup at
`H:\RadioTEDU-Backups\20261002T073533Z-runtime-release\cleanroom.db`
passed `PRAGMA integrity_check`. Previous installed source files and the
deployment manifest are retained in the same directory.

The staging application is copied from the installed application, overlaid
with the reviewed release files, and imported explicitly in isolated Python
tests. Verify the staged Python suite and all JavaScript tests before deployment.
Check SHA-256 values before copying and after replacement; abort if installed
files changed after staging. Compile the installed Python modules, restart
`RadioTEDU.OnAir.Supervisor`, and confirm new worker PIDs for all eight stations.
Read back the output roster and saved autostart/advertising settings.

## Delivery evidence

Decode all enabled outputs with the managed FFmpeg build. An open TCP socket,
HTTP status, worker heartbeat, or successful encoder write alone is insufficient.
Do not reduce quality or disable outputs to obtain a passing check.

Retain failed 24-hour verification runs. Start a new independent verification
after the final deployment has stabilized; any restart, generated silence,
dropped PCM, source error, codec mismatch, or decoder interruption prevents
that run from passing. A short successful sample is not a 24-hour guarantee.

Before deployment, Energize returned zero decoded bytes from the configured
origin. Recheck after deployment and record a continuing origin failure
separately from the local software fixes. Never label the whole task complete
while an enabled output fails or the 24-hour observation is incomplete.

## Rollback

Stop the Supervisor, restore only the files listed in the deployment manifest,
remove newly introduced files only when the manifest records their previous
absence, recompile, and restart. The database migration is additive; do not
overwrite newer operator or playout state with the old database as part of a
source rollback. A database restore requires a separate recovery decision.
