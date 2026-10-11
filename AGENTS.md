# RadioTEDU OnAir agent instructions

Before changing this repository, read `docs/AI_MAINTAINER_FUNCTION_MAP.md`. It is the canonical function-to-file, test, deployment, and rollback map.

Required safeguards:

- Preserve the dirty worktree and unrelated operator changes.
- Make and integrity-check an SQLite online backup before live configuration or schema work.
- Prefer durable UI controls with persisted read-back for routine operator tasks.
- Never create or restore a `/radiotedu` mount.
- Never source `/en` or `/fr` from this OnAir instance.
- Current protected production mode sources exactly six unsuffixed mounts from this PC: `/classic`, `/lofi`, `/radio`, `/cazz`, `/rock`, and `/energize`, all Opus 192 kbps. Keep every `-low`, `-normal`, `-high`, and `-flac` quality output persisted disabled until the operator explicitly requests re-enabling them. Origin listener health is evidence only and must never auto-disable a source.
- Do not expose protected `.env`, vault, token, password, or signing-secret values.
- Do not call a mount live from TCP connectivity alone; require decoded audio bytes.
- Run focused Python tests and all JavaScript tests before restarting `RadioTEDU.OnAir.Supervisor`.
