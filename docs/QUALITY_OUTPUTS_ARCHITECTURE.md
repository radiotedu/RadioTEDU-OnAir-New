# RadioTEDU quality-output architecture

## Contract

RadioTEDU OnAir produces 14 local music sources. The normal stream is the suffix-free primary output at Opus 192 kbps. Every music station adds a `-low` Opus 32 kbps branch. Classical and Cazz alone add Ogg/FLAC lossless branches.

| Station | Outputs sourced by this PC |
|---|---|
| RadioTEDU | `/radio`, `/radio-low` |
| Lo-Fi | `/lofi`, `/lofi-low` |
| Classical | `/classic`, `/classic-low`, `/classic-flac` |
| Cazz | `/cazz`, `/cazz-low`, `/cazz-flac` |
| Rock | `/rock`, `/rock-low` |
| Energetic | `/energize`, `/energize-low` |

The external `/en` and `/fr` AI sources are outside this fan-out. Together they form the 16-mount public system.

## Fan-out model

Each station has one authoritative programme producer. The primary Opus 192 sink and its approved quality sinks consume the same PCM timeline. A quality branch may reconnect independently without restarting, seeking, or advancing the programme.

Quality settings store only mount, profile, enabled/public flags, and `credential_mode=inherit_legacy_output`. Host, port, source username, and password are inherited in memory from the protected primary station output and are never copied to quality settings or API responses.

## Canonical policy

| Output | Codec profile | Use |
|---|---|---|
| suffix-free | `opus_192`, 192 kbps | normal/default listener stream |
| `-low` | `opus_32`, 32 kbps | constrained connections |
| `-flac` | `ogg_flac_lossless` | Classical and Cazz only |

The configuration writer knows the former `-normal`, `-high`, and all-station FLAC names solely so it can remove them. They are not valid active products.

## Commissioning invariants

- The six primary mount names must already match the canonical table; a mismatch aborts commissioning.
- Primary outputs are enabled and normalized to Opus 192 without changing host, port, source credentials, local device, gain, or protocol.
- All six station autostart flags are enabled so service startup restores the actual stream workers.
- An integrity-checked SQLite backup is completed before mutation.
- The saved quality settings are read back byte-for-byte.
- External AI quality bridging remains empty and legacy-only.
- A finished song creates one compliance event with a delivered-variant snapshot, regardless of branch count.

## Capacity and health

The origin needs at least 16 simultaneous source slots; 20 is recommended for operational headroom. A configured number is not proof. Capacity is verified only when every enabled local branch has delivered audio. Encoder, queue, and delivery health are reported per branch.

## Failure boundaries

| Failure | Required behavior |
|---|---|
| One mount rejected | retry that sink; preserve producer and siblings |
| Origin offline | continue deterministic playout and independently reconnect every enabled source |
| Encoder failure | mark only the affected branch unhealthy |
| Service restart or boot | delayed-auto supervisor restores all six authorized workers |
| Invalid/stale output settings | replace canonical quality family and retain unrelated outputs |
| Commissioning error | fail closed; recover from the verified pre-change database backup |

Implementation: `app/services/quality_outputs.py`, `app/engine/runtime_registry.py`, and `app/api/streaming.py`. Commissioning: `tools/commission_quality_outputs.py`.
