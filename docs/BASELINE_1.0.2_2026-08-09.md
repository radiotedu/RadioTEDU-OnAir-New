# RadioTEDU OnAir 1.0.2 Pre-Change Baseline

Captured read-only on 2026-08-09 in the Europe/Istanbul time zone. This
artifact contains no credential values. It is evidence for planning and is not
itself a recovery package.

## Source and release state

- Repository commit: `e36239d42607b6be36183d41fcc5b7cd1e7104b3`
- Branch: `main`
- Installed display version: `1.0.1`
- Source/API/installer version before remediation: `1.0.0`
- Large pre-existing untracked archive `__rar_18988.46402`: preserved and out
  of scope.
- Pre-code design artifacts:
  `ARCHITECTURE_1.0.2.md`, `THREAT_FAILURE_ANALYSIS_1.0.2.md`, and
  `IMPLEMENTATION_PLAN_1.0.2.md`.

The separate rtAI application is outside this RadioTEDU checkout. Its files,
configuration, and data are treated as user-owned and are not included in the
RadioTEDU baseline or modified by this plan.

## Installed process and start ownership

- One loopback backend on TCP 8100, one interactive desktop agent, and two
  FFmpeg processes were active at capture time.
- Optional Windows services running as LocalSystem:
  `RadioTEDU.JukeLocalMediaAgent`, `RadioTEDU.SharedAI`, and
  `RadioTEDUVotingRadio`.
- Two enabled legacy Classical scheduled tasks remain. They represent a
  duplicate-start risk at reboot and must be exported into the recovery
  manifest before they are disabled. They must not be deleted.
- Previously retired installations remain under the protected Recovery tree.
  No retired or legacy program may be deleted until 1.0.2 completes its full
  stability gate.

## Database and durable state

- Database: SQLite 3.49.1, WAL, `synchronous=FULL`, schema version 16.
- `quick_check`: `ok`.
- Tables: 48.
- Stations: 7; outputs: 8; tracks: 37,462; queue items: 4,359; users: 1;
  user sessions: 453; operation logs: 14,673; recovery points: 70.
- Queue state: 2,654 failed, 1,646 done, 58 pending, and 1 playing. Station 2
  owns 4,308 queue rows.
- `foreign_key_check`: nine violations, all orphaned
  `user_role_assignments` rows referencing missing `users` rows. No other
  foreign-key violation was observed.
- Five station credential references are stored in SQLite. Their DPAPI-backed
  values were verified decryptable without printing them.

## Security and storage findings

- The DPAPI credential vault ACL allowed only the explicit user,
  Administrators, and SYSTEM; Built-in Users was absent.
- The JWT signing material and plaintext initial-admin handoff file inherited
  a broad Built-in Users Modify ACE. Exposure cannot be disproved, so 1.0.2
  must protect the full data tree and rotate JWT signing material after the
  database repair.
- Disk C: approximately 23.4 GB free of 1.013 TB, or 2.31 percent free. Build,
  backup, migration, and soak activity must enforce headroom gates and bounded
  retention.

## Broadcast evidence

- The public Lo-Fi station recovered from degraded to live during observation.
- A direct listener probe to `https://stream.radiotedu.com/lofi` returned HTTP
  200 with `audio/aac` and delivered 69,616 bytes across 17 chunks.
- Other configured stations were stopped at capture time.
- The optional voting service repeatedly logged invalid MP3 framing warnings;
  optional-service failure was not observed to stop the live Lo-Fi output.

## Baseline verification

- Python partition baseline: 950 passed, 18 product failures, 3 skipped, and 3
  subtests passed. Two additional smoke failures were caused only by the
  stripped test runner lacking `WINDIR`; both passed in the native shell.
- All 18 product failures were stale assertions for the retired parallel static
  UI, not backend/runtime failures. They must be rewritten against the unified
  `app/static/onair` surface rather than restoring the retired UI.
- JavaScript: 35 passed; 11 file-level failures all referenced retired static
  assets. Browser JavaScript syntax checks passed for all 10 current files.
- .NET Release tests: 50 passed.
- Installer static scripts: 3 passed.

## No-mutation statement

At baseline capture no live database, secret, ACL, service, scheduled task,
installed binary, stream configuration, media file, or legacy program was
changed. Initial implementation and tests use isolated repository and temporary
paths only.
