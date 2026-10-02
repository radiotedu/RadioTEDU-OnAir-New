# Encoded audio forwarding and PCM clock

## Changes

The Icecast connector now uses `BufferedReader.read1()` when available, so
compressed audio is forwarded as soon as the encoder pipe has data. A fallback
to `read()` keeps existing file-like transport adapters compatible.

The PCM writer preserves its absolute media deadline through short scheduling
delays. Catch-up is limited to 250 ms (48 KiB of stereo s16 PCM at 48 kHz);
longer stalls rebase the clock instead of draining a multi-second FIFO in a
burst. This does not remove accepted programme frames or change codecs.

Health snapshots expose `encoded_pipe_read_policy` and
`pcm_clock_max_catchup_seconds`, allowing deployment checks to confirm that a
running worker has loaded this implementation.

## Local diagnostic evidence

Actual managed FFmpeg was used with six seconds of silent PCM and each enabled
codec family, without connecting any source mount:

| Profile | Encoded bytes in either read mode | First `read(4096)` return | First `read1(4096)` return |
| --- | ---: | ---: | ---: |
| AAC LC 192 | 144896 | 0.781 s | 0.860 s |
| HE-AACv2 64 | 49152 | 0.500 s | 0.094 s |
| Ogg FLAC lossless | 3142 | 6.000 s, after encoder EOF | 0.172 s |

In a separate local real-encoder clock diagnostic, the old clock drained
25.5147 seconds of PCM in 27.0780 wall seconds. The changed clock drained
27.0080 seconds in 27.0000 wall seconds. These are short local measurements;
they do not establish 24-hour public delivery continuity.

The existing focused sink recovery suite passed all 10 tests. Full broadcast
verification still requires all 16 commissioned outputs, actual decoded audio,
unchanged output contracts, and the complete 24-hour duration. Source health
alone must not be presented as listener verification.

## Deployment and rollback

Back up the live `app/audio/icecast_audio_sink.py`, replace only that source
file, and reload isolated station workers one at a time. Do not restart the
backend or Supervisor solely to load this module. Check the two new health
fields in every output before treating a worker as updated.

Rollback restores the backed-up source and reloads only the affected workers.
No schema migration, advertisement setting, codec, bitrate, or output enable
change is included in this update. Preserve newer database state on rollback.
