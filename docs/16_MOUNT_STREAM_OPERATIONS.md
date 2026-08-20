# RadioTEDU 16-mount stream operations

## Ownership

- `RadioTEDU.OnAir.Supervisor` owns the backend and six local music workers.
- This broadcast PC sources 14 mounts: six normal Opus 192, six low Opus 32, and two FLAC.
- External systems retain ownership of `/en` and `/fr`.

## Commission

Run from an elevated PowerShell prompt:

```powershell
python .\tools\commission_quality_outputs.py `
  --backup-root "C:\ProgramData\RadioTEDU\OnAir\backups\quality-commission"
```

The command must report `quality_mounts: 8`, `station_autostart_enabled: 6`, `legacy_mounts_changed: false`, and a backup SHA-256. It also disables retired `-normal`, `-high`, and non-approved FLAC definitions.

## Install or repair boot startup

```powershell
powershell -ExecutionPolicy Bypass -File .\tools\Install-RadioTEDU-OneShot.ps1
sc.exe qc RadioTEDU.OnAir.Supervisor
sc.exe query RadioTEDU.OnAir.Supervisor
```

Expected service state:

- start type is automatic (not delayed), so source startup begins during boot;
- the service runs as the machine-owned supervisor, not as a desktop login task;
- failure recovery restarts the supervisor;
- all six `broadcast_autostart_enabled` settings are `true`;
- the desktop app may be closed without stopping streams.

## Verify

1. Open `/app` and run Quality outputs diagnostics.
2. Confirm the six primary rows report Opus 192.
3. Confirm all six low rows report Opus 32.
4. Confirm FLAC exists only for `/classic-flac` and `/cazz-flac`.
5. Confirm 14 of 14 enabled local mounts deliver decoded audio.
6. Confirm `/en` and `/fr` remain externally owned.
7. Reboot the machine and verify the service and all six workers recover without opening the desktop app.

## Rollback

Disable only the quality outputs from the operator wall or rerun commissioning with `--disabled`. This preserves primary streams, credentials, settings, media, compliance records, and backups. If database recovery is required, stop the supervisor first and use the integrity-checked backup produced immediately before commissioning.
