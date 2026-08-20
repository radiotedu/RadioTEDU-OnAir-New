# RadioTEDU OnAir 1.0.2 threat and failure analysis

Status: implementation gate
Prepared: 2026-08-09

This analysis covers safety, availability, integrity, confidentiality, and
recoverability. Severity is the worst credible operational consequence;
likelihood reflects the inspected installation before 1.0.2 work.

## Assets and trust boundaries

Protected assets are uninterrupted program audio, station/output ownership,
queues and playback checkpoints, schedules, the media catalog, the SQLite
database, credentials, signing material, audit history, backups, and operator
authority.

Trust boundaries exist between:

- WebView2 content and the native shell;
- the shell and the loopback API;
- authenticated operators and administrative operations;
- the backend and each station-worker process;
- workers and decoder/encoder subprocesses;
- the machine and external Icecast/Shoutcast servers;
- core playout and optional AI/voting/request services;
- immutable Program Files and mutable ProgramData;
- the active installation and preserved legacy programs; and
- normal diagnostics and memory dumps that may contain secrets.

## Current release blockers

| ID | Finding | Severity | Required disposition |
|---|---|---:|---|
| B-01 | Nine orphaned role-assignment foreign keys exist in the active database. | High | Back up, deterministically repair through a migration, verify both integrity checks, and add a regression test. |
| B-02 | JWT and bootstrap-password files inherit Modify permission for the local Users group. | Critical | Harden ACLs, prove effective access, rotate signing material if exposure cannot be excluded, and test install/repair ACLs. |
| B-03 | The system volume has 2.31% free space. | Critical | Block build/migration/restore below safe thresholds; reclaim or relocate data only with explicit operator approval; test full-disk behavior. |
| B-04 | Two enabled legacy Classical boot tasks can launch separate playout commands. | Critical | Preserve definitions for rollback, disable conflicting automatic starts, and prove one source owner after reboot. Do not delete them before the 1.0.2 stability gate. |
| B-05 | Station workers are backend threads rather than fault-contained processes. | Critical | Introduce independent worker processes, leases, fencing, heartbeats, and supervisor-owned lifecycle. |
| B-06 | Shoutcast source output has no implementation or tests. | High | Add an explicit protocol adapter and authorized/simulated contract verification. |
| B-07 | Current source and installer metadata still contain 1.0.0 while the installed registration is 1.0.1. | High | Establish one version source and verify API, binaries, installer, registry, provenance, tag, and release notes as 1.0.2. |
| B-08 | No RadioTEDU crash-dump and redacted diagnostic-bundle facility is implemented. | High | Add bounded WER configuration, protected dump storage, and a secret-safe bundle generator. |
| B-09 | Optional voting output is producing a sustained invalid-MP3 warning storm. | Medium | Validate framing/input, rate-limit repeated diagnostics, and prove optional failure cannot affect core playout. |
| B-10 | Public Lo-Fi status moved from degraded to live during inspection. A valid media sample was obtained, but a single recovery is not a soak. | High | Run continuous evidence collection and require zero unexplained reconnects for the observation window. |
| B-11 | A large untracked archive-like file exists in the repository root. | Medium | Preserve it as user-owned data, exclude it from builds/scans where appropriate, and obtain explicit approval before moving or deleting it. |

The five active source credentials are not plaintext in the database: they are
opaque credential references, and the five-entry machine-scoped DPAPI vault was
successfully decrypted in a read-only audit. This control remains conditional
on ACL hardening and service-identity testing.

## Failure-mode and effects analysis

| Failure | Detection | Containment and recovery | Required evidence |
|---|---|---|---|
| UI closes or crashes | shell process exits | no action in audio plane; relaunch shell only | close UI during active test stream; listener bytes continue |
| Backend crashes | supervisor heartbeat deadline | workers keep current checkpoint/queue; bounded backend restart | kill backend; stream and metadata evidence remain coherent |
| One station worker crashes | process exit + heartbeat | stop its fenced children; restart only that worker from checkpoint | other station workers and outputs remain uninterrupted |
| Encoder exits | child exit + byte-progress stop | replace encoder, preserve decoder and playback offset | injected exit with no queue clearing or duplicate source |
| Half-open source TCP connection | write deadline + independent mount/listener probes | tear down source generation and reconnect with backoff | proxy/fixture drops acknowledgements or payload |
| Source rejected/wrong password | handshake status and redacted error code | circuit-break credentials; local fallback; operator action | rejection fixture; secret absent from every artifact |
| DNS/network loss | bounded resolver/connect/write timeouts | continue local fallback; jittered retry budget | DNS and network fault injection with recovery evidence |
| Remote server returns headers but no media | listener byte-progress deadline | remain degraded; recreate output only when policy permits | HTTP audio headers with zero-payload fixture |
| Media missing/corrupt/invalid | preflight probe and decoder progress | quarantine item, record reason, select deterministic fallback | malformed, missing, long-path, locked, and unsupported fixtures |
| Silence or clipping | PCM meter and configured consecutive-window thresholds | mark degraded, switch to fallback or reduce gain by policy | deterministic generated-audio fixtures |
| Decoder blocks | PCM heartbeat and read deadline | terminate decoder generation; resume checkpoint/fallback | blocking process fixture |
| PCM consumer blocks | bounded per-output queue saturation | drop/restart failed output branch; decoder continues | stalled encoder fixture |
| Database busy/locked | busy timeout and error class | bounded retry; no destructive rebuild | concurrent writer and long-reader tests |
| Database corruption | quick/integrity/foreign-key checks | fail readiness; restore verified backup to staged path | corrupted-copy restore drill |
| Migration fails | transaction exception + migration journal | rollback transaction; preserve pre-migration backup | failure at every migration step |
| Configuration corrupt | schema/hash/read failure | load last-known-good generation | truncated and bit-flipped ledgers |
| Disk critically low/full | periodic free-space and write preflight | stop nonessential writes; protect checkpoints; alert; never delete media automatically | quota/full-volume fixture |
| Log storm | per-signature rate and byte limits | coalesce/rate-limit; rotate by size and age | repeated optional-service error fixture |
| Machine reboots | service startup + reconciliation journal | start one supervisor/backend; workers honor durable desired state | real reboot acceptance test |
| Clock/timezone/DST transition | timezone-aware scheduler audit | idempotent event IDs and explicit ambiguous/nonexistent-time policy | spring/fall DST boundary tests |
| Concurrent control requests | idempotency key, transaction, fencing token | one committed transition; duplicates return prior result | parallel start/stop/config requests |
| AI/provider outage | optional-service readiness/timeout | skip cached announcement; core queue continues | disabled, timeout, crash, malformed response tests |
| Backup target unavailable | backup verification result | retain last verified backup; alert; do not claim protected | locked/missing/full target tests |
| Restore interrupted | staged restore journal | active database unchanged until atomic activation | power-loss simulation at each stage |
| Upgrade fails | installer exit and post-install readiness | retain prior binaries/data; execute rollback plan | install, upgrade, forced failure, rollback tests |

## Security threat analysis

| Threat | Control | Verification |
|---|---|---|
| Stolen source credentials | DPAPI machine scope plus protected ACLs; opaque DB references; rotation workflow | ACL test, decrypt-as-service test, denied untrusted-user test, secret scan |
| Credential leakage in logs/diagnostics | structured fields, centralized redactor, subprocess stderr redaction, bundle allowlist | seeded canary secrets never appear in artifacts |
| API privilege escalation | deny-by-default RBAC dependency on every state-changing route | permission-route matrix and negative tests |
| CSRF or hostile origin | loopback bind, authenticated mutation, origin/host validation, SameSite cookies where used, CSRF token for cookie-authenticated writes | cross-origin and forged-host tests |
| Session theft/replay | short access lifetime, hashed/rotated refresh tokens, revocation, bounded session retention | replay/rotation/revocation tests |
| Brute force | rate limit, delay/backoff, audit, no user enumeration | distributed attempt and reset tests |
| Path traversal or arbitrary file access | canonical path checks, allowed-root policies, reparse-point handling | traversal, UNC, ADS, device-path, symlink/junction tests |
| Command injection | executable allowlist, argument arrays, `shell=False`, no secrets in arguments | metacharacter and hostile filename tests |
| WebView2 native-bridge abuse | trusted-origin check, minimal message schema, standard-user host | hostile navigation/message tests |
| Malicious media parser input | preflight in constrained subprocess, time/size limits, no inherited handles | fuzz/malformed corpus and timeout tests |
| Update tampering | Authenticode-ready signing, published hash/provenance, signature verification before apply | modified payload rejection and rollback |
| Local privilege abuse | least-privileged service identity, service SID/ACL, separate elevated repair helper | effective-token and ACL inspection |
| Audit alteration | append-only hash chain with protected anchor and backup | deletion/reorder/modification detection tests |
| Backup disclosure | protected backup ACL, no plaintext secret export, encrypted credential vault retained as ciphertext | untrusted-user denial and content scan |
| Memory-dump disclosure | protected dump directory, bounded retention, opt-in support export | ACL and bundle-exclusion tests |

Python subprocess calls must use argument sequences and avoid `shell=True`, in
line with the official [subprocess security guidance](https://docs.python.org/3/library/subprocess.html#security-considerations).
WebView2 content is treated as untrusted at the native boundary, following
Microsoft's [WebView2 security guidance](https://learn.microsoft.com/en-us/microsoft-edge/webview2/concepts/security).
Machine-scoped DPAPI is paired with ACLs because Microsoft's
[`LocalMachine` scope documentation](https://learn.microsoft.com/en-us/dotnet/api/system.security.cryptography.dataprotectionscope)
states that other accounts on the machine can otherwise unprotect data.

## Restart budget and circuit-breaker policy

Every backend, worker, encoder, and optional service has its own rolling restart
budget. Defaults are configuration with conservative bounds, not hard-coded
infinite loops:

- immediate retry only for one confirmed transient probe failure;
- exponential delays with jitter and a maximum delay;
- a finite attempt count within a rolling window;
- half-open circuit probes after cooldown;
- reset only after a sustained healthy interval; and
- an operator-visible reason when the circuit remains open.

Failures in an optional module never consume a core playout restart budget.

## Data migration and rollback hazards

1. Never copy only the SQLite main file while WAL mode is active. Use the
   online backup API or a clean checkpoint/close operation.
2. Never rewrite credential references before proving that the target service
   identity can decrypt the target vault.
3. Never repair orphan rows without a deterministic mapping or explicit
   quarantine record.
4. Never remove legacy startup definitions before exporting them to the
   recovery manifest.
5. Never upgrade schema without a verified pre-migration backup and a tested
   downgrade/restore procedure.
6. Never treat a successful installer exit as proof that broadcasting works.

## Residual risk accepted only with explicit evidence

- Internet and upstream streaming-server availability are outside the single
  machine's control; local fallback and honest degraded states contain impact.
- One Windows machine and one SQLite database are not hardware high
  availability. A witness/standby feature cannot be called HA until deployed
  on independent hardware and tested under partition.
- Authenticode signing requires an organizational certificate. An unsigned
  build must be labeled unsigned and cannot satisfy the signed-production gate.
- A 24-hour facility is not equivalent to a 24-hour result. The release record
  must include the actual observation interval and all reconnect explanations.

## Threat/failure acceptance gate

Every Critical and High finding must be closed by code, automated tests, and
installed-machine evidence, or explicitly accepted in writing by the product
owner. No secret-bearing output may be used as evidence.
