# Broadcast-PC Commissioning Verifier

`tools/verify_broadcast_pc_commissioning.py` is a read-only gate for staging
the Juke, Voting, and mountless AI/public-state stack. It never installs,
starts, stops, restarts, or reconfigures a service.

It writes the evidence file only when one probe cycle passes every required
condition: exact IPv4 loopback bindings for ports 3210, 4317, and 4320; Juke
mirror/autoplay disabled; Juke WSS/heartbeat/reconnect health; Voting local
audio, WSS authentication/reconnect, connected Icecast state, and sole `/ai`
ownership; a 30-second public `/ai` decode; `/event` and EN/FR JSON endpoint
checks; mountless AI/public-state settings; and stable fingerprints for the
protected source/config references.

The generated JSON is intentionally compatible with
`installer/ProvisionBroadcastPcAgents.ps1`: it uses `schemaVersion`,
`generatedAtUtc`, and the installer-required `checks` field names. It contains
only booleans and SHA-256 fingerprints—never environment contents, URLs with
credentials, command arguments, or response bodies.

In addition to the legacy aggregate checks, the verifier emits the exact
provisioning-gate fields `publicAiDecode30Seconds`,
`publicEventEndpointChecked`, `radioTeduEnEndpoint200`,
`radioTeduFrEndpoint200`, `aiPublicStateMountless`, and
`aiPublicStateSourceFingerprintVerified`. Each is derived from the same strict
probe/fingerprint result as its corresponding aggregate check; a missing or
changed fingerprinted file can never be attested as verified.

Create an ACL-protected JSON file containing references only:

```json
{
  "music_library_path": "C:\\Music",
  "juke_health_url": "http://127.0.0.1:3210/health",
  "voting_health_url": "http://127.0.0.1:4317/api/health",
  "voting_audio_url": "http://127.0.0.1:4320/ai",
  "public_ai_url": "https://stream.radiotedu.com/ai",
  "event_url": "https://radiotedu.com/event",
  "en_status_url": "https://radiotedu.com/status/en",
  "fr_status_url": "https://radiotedu.com/status/fr",
  "ai_env_file": "C:\\ProgramData\\RadioTEDU\\ai-broadcast-agent\\config\\agent.env",
  "public_state_config_file": "C:\\ProgramData\\RadioTEDU\\OnAir\\config\\public-state-agent.json",
  "fingerprint_files": ["C:\\ProgramData\\RadioTEDU\\ai-broadcast-agent\\config\\agent.env"],
  "evidence_file": "C:\\ProgramData\\RadioTEDU\\OnAir\\Commissioning\\preflight-evidence.json"
}
```

Run it only after foreground staging is ready:

```powershell
python .\tools\verify_broadcast_pc_commissioning.py --config C:\ProgramData\RadioTEDU\OnAir\config\broadcast-pc-verifier.json
```

The verifier does not attempt recovery. Any missing listener, health signal,
unexpected binding, decode failure, configuration change, or failed ownership
claim exits nonzero and preserves the previous evidence file.
