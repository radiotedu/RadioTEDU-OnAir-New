# RadioTEDU no-dead-air repair — 2026-08-11

## Live result

- All six legacy stations run in isolated Python workers with a 512-chunk PCM
  queue, independent liveness heartbeat, current-program transport checks, and
  zero dropped PCM chunks after the rolling transition.
- The configured listener base remains
  `http://stream.radiotedu.com:11154`. Classical, Lo-Fi, Pop, Jazz, Rock and
  Energize each delivered a paced 10-second audible sample concurrently.
- The five-minute Windows audio watchdog probes all six configured listener
  mounts concurrently and in real time. It requires at least 7.5 seconds of
  decoded media, confirms failures twice, and does not restart a station when
  local program PCM and the source writer remain healthy.
- The latest scheduled run completed successfully with all six streams audible
  and managed profiles healthy.
- No installer or release package was created.

## Repaired local failure modes

1. A full 64-chunk writer queue previously discarded the entire buffered
   timeline, causing audible jumps. The queue is now 512 chunks and a saturated
   write drops at most the current chunk while forcing controlled sink recovery.
2. A ten-second adopted-worker heartbeat threshold spawned duplicate source
   owners. The threshold now matches the 60-second stall window and a fenced,
   unresponsive adopted PID is terminated before replacement.
3. Scheduler heartbeat publication previously depended on completion of a full
   DB/queue/AI tick. A separate liveness writer now preserves healthy encoder
   audio while still allowing replacement when both scheduler and current PCM
   are unhealthy.
4. Runtime status scanned AI cache JSON files synchronously. Slow storage could
   block music startup, liveness and watchdog status together. Broadcast status
   now uses persisted AI readiness; explicit AI diagnostics own fresh scans.
5. API startup waited for dependency bootstrap, compliance export and six
   sequential worker starts. These non-critical tasks now run in bounded
   background threads after DB initialization.
6. Source-mode development startup did not explicitly enable isolated workers.
   The launcher now sets `RADIOTEDU_PROCESS_ISOLATED_WORKERS=1`.
7. The watchdog used the broken HTTPS proxy instead of the configured TinyIce
   listener base and ran six probes serially. It now uses the direct configured
   base and parallel paced probes, so proxy faults cannot create a source
   restart storm.

## Remaining upstream TLS action

The certificate served by `stream.radiotedu.com:443` is expired:

- subject: `CN=stream.radiotedu.com`
- issuer: Let's Encrypt `R13`
- valid from: `2026-05-06 18:29:23 UTC`
- expired: `2026-08-04 18:29:22 UTC`
- measured on 2026-08-11: expired by 7 days

This Windows account cannot administer `RadioTEDUYayin` (`10.98.98.75`): remote
Service Control Manager and Task Scheduler queries both returned Access Denied.
Do not weaken TLS validation and do not replace the existing renewal definition
blindly.

On `RadioTEDUYayin`, an administrator should:

1. Identify the existing ACME client and renewal definition. If it is win-acme,
   inspect its scheduled task, Event Viewer entries, and
   `%ProgramData%\win-acme\...\Log` before changing anything.
2. Correct the recorded renewal/installation failure. For an existing win-acme
   renewal, its official troubleshooting command is
   `wacs.exe --renew --force --verbose`; use the installed executable and its
   existing configuration directory.
3. Confirm that the successful renewal installs the new certificate/key into
   the files or certificate store actually referenced by Nginx. Validate the
   current Nginx configuration before reload, then perform the configured
   graceful reload.
4. Verify the presented certificate expiry from another machine and require all
   six HTTPS mounts to deliver audible media continuously for at least 60
   seconds. Keep the direct TinyIce mounts online throughout this work.
5. Repair/recreate the automatic renewal task only after the existing renewal
   is understood, then monitor the next automatic run and configure failure
   notification.

Official win-acme references:

- <https://www.win-acme.com/manual/automatic-renewal>
- <https://www.win-acme.com/reference/cli>
- <https://www.win-acme.com/reference/plugins/installation/script>

## Verification evidence

- Targeted reliability suite: 85 passed, followed by 23 targeted passes and the
  corrected watchdog contract pass.
- Multi-quality/runtime/startup group: 32 passed.
- PowerShell watchdog parser: clean.
- `git diff --check`: clean apart from repository line-ending notices.
- Live watchdog: latest result `0`, all six streams audible.
- Live workers: all six `program=true`, `scheduler_stalled=false`,
  `transport=true`, `pcm_queue_capacity_chunks=512`, `backpressure=false`, and
  `dropped_pcm_chunks=0`.
