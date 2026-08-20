# Broadcast-PC agent provisioning

`installer/ProvisionBroadcastPcAgents.ps1` provisions the separately deployed
Juke Local, Voting Radio, and AI/public-state agents through the repository-owned
`RadioTEDU-OnAir-ServiceHost.exe`. It is intentionally fail-closed.

Before provisioning, use the separate elevated staging helper. It prepares
sources and protected configuration but intentionally does not start, stop,
register, or modify a service or scheduled task:

```powershell
& 'C:\Program Files\RadioTEDU\OnAir\installer\StageBroadcastPcHandoff.ps1' -HandoffRoot 'C:\RadioTEDU-Handoff' -JukeMusicRoot 'D:\RadioTEDU\music' -VotingMusicRoot 'D:\RadioTEDU\music' -WhatIf
```

`StageBroadcastPcHandoff.ps1` requires source snapshots, Node lockfiles, the
packaged `installer\requirements\radiotedu-handoff-py312.lock.txt` manifest,
and nonempty operator music roots. It validates every dependency as pinned and
SHA-256 hashed, copies that exact manifest into staged RadioTEDU as
`requirements.lock` for provenance, and rejects any substitute that is not
fully pinned and hashed. It
locates installed Node, Python, FFmpeg, and FFprobe runtimes and rejects any
runtime copied from the handoff or a user profile. It stages versioned copies
under `C:\RadioTEDU\.staging`, installs clean dependencies with `npm ci` and
`pip --require-hashes`, then publishes each tree by same-volume rename. Existing
nonempty targets are rejected unless `-ReplaceExistingTargets` is explicit; in
that case the old target is moved into `C:\RadioTEDU\.rollback` rather than
deleted.

FFmpeg and FFprobe may be discovered from a per-user installation such as Winget,
but they are never used from that location by a LocalSystem agent. Staging
validates the input binaries, copies them through the versioned staging area,
then atomically publishes protected exact files at `C:\RadioTEDU\tools\ffmpeg.exe`
and `C:\RadioTEDU\tools\ffprobe.exe`. The target directory must have a protected
SYSTEM/Administrators-only ACL; reparse points and user-writable targets are
rejected. Juke and Voting configuration receive only these service-safe paths.

The staging helper writes only protected ProgramData configuration. It remaps
the music and tool locations, enforces Juke loopback port 3210 and disables its
AI mirror/autoplay, and enforces Voting loopback binds for ports 4317 and 4320.
It does not echo environment values, URLs, credentials, or command arguments.

Before staging, create and retain a whole-bundle integrity manifest. The generator
is deterministic and records only normalized relative paths, byte sizes, and
SHA-256 digests; it never reads or prints secret values. For the supplied handoff:

```powershell
& 'C:\Program Files\RadioTEDU\OnAir\installer\NewBroadcastPcHandoffManifest.ps1' -HandoffRoot 'C:\RadioTEDU-Handoff' -ManifestPath 'C:\RadioTEDU-Handoff\handoff-manifest.json'
```

The manifest excludes `secrets` directories, `.env`/key/certificate/credential
files, the manifest itself, and reproducible/generated directories: `node_modules`,
`.venv`, `dist`, `build`, `.cache`, Python/test caches, coverage, frontend caches,
and logs. It rejects reparse points and duplicate/case-colliding paths. The staging
helper validates the manifest before it creates a staging directory and fails on
any missing, extra, size-mismatched, or SHA-256-mismatched non-secret input file.

Run from an elevated PowerShell session after the handoff snapshot has been
deployed to the fixed paths described in the Broadcast-PC migration prompt.
Always review the dry run first:

```powershell
& 'C:\Program Files\RadioTEDU\OnAir\installer\ProvisionBroadcastPcAgents.ps1' -WhatIf
```

The helper validates the required `C:\RadioTEDU` layout, handoff source/config
files, protected ProgramData configuration directories, ServiceHost binary,
Node runtime, and each agent runner. It requires a current, secret-free JSON
evidence file at `C:\ProgramData\RadioTEDU\OnAir\Commissioning\preflight-evidence.json`.
Its schema is deliberately small:

```json
{
  "schemaVersion": 1,
  "generatedAtUtc": "2026-08-01T00:00:00Z",
  "checks": {
    "operatorMusicLibraryPresent": true,
    "jukeForegroundPassed": true,
    "jukeLoopback3210": true,
    "jukeWssConnected": true,
    "jukeHeartbeat2xx": true,
    "jukeReconnectPassed": true,
    "votingForegroundPassed": true,
    "votingLoopback4317": true,
    "votingLoopback4320": true,
    "votingWssAuthenticated": true,
    "votingReconnectPassed": true,
    "votingIcecastConnected": true,
    "publicAiDecode30Seconds": true,
    "publicEventEndpointChecked": true,
    "radioTeduEnEndpoint200": true,
    "radioTeduFrEndpoint200": true,
    "votingSoleAiSource": true,
    "aiPublicStateMountless": true,
    "aiPublicStateSourceFingerprintVerified": true
  }
}
```

Do not add endpoint URLs, credentials, tokens, passwords, headers, or full
command lines to that file. Evidence older than 24 hours is rejected by default.

The installer packages the repository-owned mountless
`tools\radiotedu_public_state_agent.py`. Provisioning writes its reference-only
JSON configuration at `C:\ProgramData\RadioTEDU\ai-broadcast-agent\config\public-state-agent.json`
and runs it with the staged RadioTEDU virtual-environment Python. The agent has
no playout or network-source command line and must not create an Icecast `/ai`
source. Juke mirror/autoplay settings must remain false and Voting is required
to be the only `/ai` source owner. The helper creates three protected five-field
`.services` files and SCM services with delayed automatic startup and bounded
recovery (5, 15, 60 seconds). It does not start them.

The evidence must come from the commissioning verifier and attest to a public
`/ai` decode of at least 30 seconds, an independent `/event` check, individual
English and French status endpoint checks, Juke heartbeat/WSS reconnect, Voting
authentication/reconnect/Icecast checks, sole `/ai` ownership, and both the
mountless-agent and source-fingerprint checks. It must remain reference-only:
do not add any URL with credentials, token, password, secret, header, or full
process argument to evidence or the public-state JSON configuration.

Existing SCM services are not altered unless `-ReplaceExistingServices` is added
after an operator review. The script never copies secret files and never logs
environment values or ServiceHost child arguments.
