# High-Assurance OnAir Deployment

This deployment profile provides deterministic fail-silent behavior, witnessed
active/passive operation, independently verified media, transactional stream
changes, and auditable recovery evidence. It is not a NATO, military, SIL, or
five-nines certification.

## Topology

- Two equivalent Windows broadcast PCs (`onair-a` and `onair-b`).
- One independent witness host running `app.witness`.
- One stable HTTPS name mapped to a floating LAN address.
- Independent local media disks; do not use one shared disk as the only copy.

The two OnAir nodes and witness are three voters. A leader renews a six-second
lease every second and needs one external acknowledgement. A node that cannot
maintain quorum releases the floating address, stops worker loops, closes
Icecast output, and closes the current recording segment. The promoted node
continues an acknowledged recording in a new segment. This prevents two
partitioned nodes from transmitting simultaneously.

## Pairing and configuration

1. Install the identical signed OnAir bundle on both broadcast PCs.
2. Configure a unique `CLEANROOM_HA_NODE_ID` on each PC.
3. Generate a long random `CLEANROOM_HA_SHARED_SECRET` and provision it through
   protected configuration on both PCs and the witness. Never place it in a
   repository, handoff archive, or diagnostic output.
4. Configure `CLEANROOM_HA_PEERS` with the other OnAir node and witness URL.
   Synchronize all three hosts to the same authenticated time source. Internal
   HA credentials rotate every 30 seconds and tolerate one prior interval.
5. Configure the same floating IP and Windows interface name on both broadcast
   PCs. The service account requires permission to add and remove that address.
6. Provision the Icecast source password through each node's stream wizard so
   DPAPI protects it independently on both machines.
7. Run the witness with an isolated database:

   `py -m uvicorn app.witness:app --host 0.0.0.0 --port 8110`

8. Enable HA on the standby first, verify `/api/ha/status`, then enable it on
   the intended primary. Only a node reporting `safe_to_broadcast: true` may
   own the floating address or start station workers.

## Media mirroring

Use `tools/onair_media_mirror.py SOURCE DESTINATION` while the destination node
is out of service. Files are copied to a staging directory, hashed, and promoted
atomically only after the manifest matches. Use `--check` for a read-only
commissioning check. A hardlink or shared NAS path is not an independent copy.

## Recovery points

OnAir creates DPAPI-protected and integrity-checked SQLite recovery points on
Windows: 48 hourly, 30 daily, and 12 monthly. Recovery creation never replaces
the live database. Commissioning must include a restore verification on an
isolated path and an offline copy outside both broadcast PCs.

An administrator may stage a previously verified point with
`POST /api/recovery/points/{id}/stage-restore`. Staging decrypts the protected
point, verifies its stored SHA-256, SQLite integrity, and foreign keys again,
then atomically publishes `State\Recovery\pending.json`. It does not modify the
active database.

Place the station in an announced maintenance window and restart the RadioTEDU
supervisor. Before starting the backend, the supervisor verifies the staged
database hash, copies the current database to `Backups`, atomically activates
the staged database, removes stale WAL/SHM sidecars, and records evidence below
`State\Recovery\Completed`. On an application error it restores the prior
database and emits a bounded failure artifact. Never hand-edit the pending plan
or copy a database over the live file.

## Guest recording mirror

Set `CLEANROOM_RECORDING_MIRROR_ROOT` to storage independently presented by the
standby. Program PCM is queued to separate FLAC encoders for the local and mirror
copies. A slow or failed mirror is removed and audited without blocking the
broadcast thread. Guest-room token hashes, safe room state, consent decisions,
and recording manifests are journaled to the standby; recovered guests always
start disconnected and off-air.

## Safe stream changes

The operator wizard writes a versioned draft, distinguishes validation evidence,
requires a reachable destination and protected credential, preserves the prior
configuration, applies once with an idempotency key, and reads authoritative
state back. Failed application restores the last-known-good configuration and
restarts the prior live input when it was running. A running output must remain
healthy throughout the 60-second observation window before the operation is
reported as applied.

## Standby-first upgrade

Keep the prior signed application bundle and a schema-compatible recovery point.
Upgrade the standby, verify its media manifest, database restore, health endpoint,
and isolated Icecast output, then transfer the role and observe it before upgrading
the former leader. Roll back to the retained bundle and recovery point if any gate
fails; do not perform simultaneous in-place upgrades.

## Commissioning gates

- Use a non-production Icecast mount with approved content.
- Run 100 automated role-transfer cycles and verify there is never more than
  one Icecast source or floating-IP owner.
- Kill the active backend, disconnect each network leg, stop the witness, fill
  the recording disk, corrupt a copied database, remove queued media, reject
  Icecast credentials, and disable TURN. Record the observed recovery result.
- Demonstrate active-node recovery within 15 seconds on the commissioned LAN.
- Complete a seven-day playout soak on the designated hardware.
- Have at least five non-technical operators complete the stream wizard without
  opening Advanced settings or exposing credentials.

Availability and recovery targets must not be advertised as achieved until the
commissioning evidence is reviewed and signed by the responsible organization.
