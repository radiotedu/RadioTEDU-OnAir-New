# Public State Agent

`tools/radiotedu_public_state_agent.py` is a local, fail-closed mirror for
operator health/reporting. It never connects to an audio mount and never starts
or controls playout processes. In particular, it does not accept source
credentials and makes no network requests or writes.

The agent dynamically imports the deployed
`C:\RadioTEDU\RadioTEDU\backend\public_sync.py` and requires
`PublicSyncService` to expose one of these path-only contracts:

- `get_public_state_paths()`
- `configured_public_state_paths()`
- `public_state_paths`

The contract must return absolute paths for EN/FR status JSON, EN/FR history
JSON, and the public database. The supported key names are documented in the
source. If the deployed service changes, lacks the contract, returns an unsafe
path, or its source/config/env reference changes while this process runs, the
agent stops without publishing a new state file.

The protected JSON configuration contains references only—never credentials:

```json
{
  "api_origin": "https://radiotedu.com",
  "backend_root": "C:\\RadioTEDU\\RadioTEDU",
  "backend_env_file": "C:\\ProgramData\\RadioTEDU\\OnAir\\config\\public-sync.env",
  "state_file": "C:\\ProgramData\\RadioTEDU\\OnAir\\State\\public-state.json",
  "log_file": "C:\\ProgramData\\RadioTEDU\\OnAir\\Logs\\public-state-agent.log",
  "poll_seconds": 5
}
```

Keep the configuration and referenced environment file ACL-protected. The
agent fingerprints the configuration, environment reference, and deployed
`public_sync.py` at startup and verifies them before every snapshot.

Preflight is intentionally side-effect free:

```powershell
python .\tools\radiotedu_public_state_agent.py --config C:\ProgramData\RadioTEDU\OnAir\config\public-state-agent.json --check
```

`--once` creates one atomic, secret-redacted state file. Without `--once`, the
agent retries transient local-read errors with exponential backoff capped at 60
seconds and exits immediately on a security/configuration violation.
