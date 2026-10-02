# Cache retention and broadcast recovery — 2026-10-02

## Deployed changes

- Consumed SSD cache entries remain reusable inside the existing 4 GiB LRU budget.
- Cache metadata touches and cleanup requests are throttled to once per minute per process. Warm prefetches do not create new threads.
- Decoder startup reads the original media immediately when the cache is cold; copying runs in the background.
- Serial worker reloads use the watchdog API and verify primary and enabled auxiliary outputs. A new PID and fresh heartbeat are required; manager generation numbers can reset.
- The delivery verifier checks the persisted live output roster, actual worker PIDs, per-output source counters, received codec/bitrate and decoded listener PCM. Windows SYSTEM workers are checked through PID enumeration when a query handle is denied.

The live deployment retains eight station workers and sixteen enabled outputs, including HE-AACv2 and FLAC variants. No codec or bitrate settings were reduced.

## Main Character recovery

The separate scheduled keeper was launching a Desktop checkout and a WinGet FFmpeg build. It reported an unsupported `afterburner` option and switched encoders. Listener decoding also reported corrupt AAC packets.

Its scheduled-task XML and launcher were backed up before disabling that keeper. Main Character now starts through the existing OnAir Supervisor service, using the same configured FFmpeg build as the other stations. The persisted `broadcast_autostart_enabled` setting was already true for all eight stations. A twelve-second decoded listener sample after migration contained valid AAC LC audio at approximately 192 kbps with no decoder errors.

## Evidence and limits

- Focused Python suite: 54 passed. Existing JavaScript suite: 80 passed.
- Online SQLite backup: integrity check passed, approximately 257 MiB.
- Short physical-disk samples measured C: writes at 1.2674 MiB/s before deployment and 0.8815 MiB/s later. These intervals include OS and other application activity and do not establish a sustained reduction. Process I/O counters include pipes and network traffic and are not disk-write totals.
- The public origin status listed all sixteen commissioned mounts. This alone does not prove delivery or uninterrupted audio.
- Twelve-second listener probes decoded valid audio from fifteen of the sixteen outputs, including both FLAC variants and all HE-AACv2 variants. `/energize` returned no decoded audio.
- Energize continued to show source-send timeouts after normal and delayed restarts. Both its source-port HTTP listener and public HTTPS listener timed out. During a confirmed local stop and a 25-second release window, the origin still listed `/energize`. The origin is a different host from this PC. Server access is needed to investigate stale state, another source, or server backpressure; the exact cause has not been conclusively diagnosed.
- Some auxiliary outputs have accumulated generated-silence counters at transitions. The verifier treats increases and resets as failures; these are not waived by a healthy mount status.

## Independent delivery observation

The local scheduled task `RadioTEDU OnAir Delivery Verification` launches `pythonw -m tools.run_live_delivery_soak` outside Codex. It records state, the authoritative output manifest and decoded evidence under `C:\ProgramData\RadioTEDU\OnAir\Diagnostics\live-delivery-soak`.

The startup gate requires stable sources. With the explicit `--observe-unready` option, observation continues after the startup deadline so a broken output cannot prevent collecting evidence for the other outputs. That failure remains in the final result permanently. A running task or a successful short sample is not a completed 24-hour result. The continuity goal remains active until a full measured interval passes, and current source/listener failures remain unresolved evidence.

Local backup roots:

- `H:\RadioTEDU-Backups\2026-10-02T05-13-25-242Z-fast-cache-retention`
- `H:\RadioTEDU-Backups\2026-10-02T-maincharacter-supervisor`

The earlier `.part` database backup is incomplete and must not be used for restoration.
