# Unattended broadcast recovery

On Windows, `RadioTEDU.OnAir.Supervisor` runs as LocalSystem with delayed
automatic startup and the TCP/IP service dependency. It starts without an
interactive login. SCM recovery restarts the host after 5, 15 and 60 seconds;
the final action repeats. The service host independently restarts exited child
processes with capped backoff. The installer already provisions these settings;
verify them when upgrading an existing installation.

Each commissioned station must have `broadcast_autostart_enabled=true` saved
in SQLite. Do not enable dormant stations or change output credentials,
mounts, codecs, bitrates, quality branches or library roots to repair boot.

Cold-start recovery retries a temporarily locked database or failed station
without a finite attempt limit. Each pass closes its connection before waiting,
uses a fresh connection, and selects only previously failed stations. Retry
delays are capped at 30 seconds. Re-read authorization and media paths before
starting; disabling automatic restart cancels that station's pending recovery.
Application shutdown interrupts the wait. Successful boot ends this task;
normal station supervision then owns recovery.

Icecast outputs already reconnect independently with bounded connect/write
deadlines and unlimited capped backoff. An absent internet connection or origin
does not disable a saved output. When connectivity returns, the output connector
resumes the configured encoder/profile. This cannot transmit while the network
or origin is unavailable; accepted PCM is retained in bounded queues. Internet
recovery does not require restarting all stations. The surviving PCM writer clears
its failed-pipe state only after observing a different live encoder pipe, then
retries the same retained frame. A dead writer still requires supervised recovery.

`tools/verify_network_recovery.py --ffmpeg PATH --output-dir OWNED_TEST_DIRECTORY`
exercises both offline start and an established source outage with a real encoder
and decodes the recovered audio. It binds only to loopback.

Verify recovery using an isolated database and loopback test origin, including
startup before the origin is available and an established-connection outage.
Never disable the production network adapter or reboot Windows just to test.
After a native package upgrade, decode fresh audio from every configured quality
output and check PCM drops, encoder/network errors and fallback status.

Keep an integrity-verified online SQLite backup, original SCM settings, previous
native service definition and Git bundle. Revert code by restoring its service
definition; do not roll back a live database merely to revert application code.
