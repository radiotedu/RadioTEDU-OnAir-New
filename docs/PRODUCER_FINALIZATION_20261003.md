# Keep owned audio while an exited decoder's PCM pipe finishes

The live source recording from 01:07:55 UTC on 3 October shows PowerAPP's
elapsed clock resetting three times. A read-only database inspection confirms
that these were the same ad row (518), which remained owned from 01:13:46 to
01:14:33 UTC. They were not separate scheduled spots.

`program_running` describes the decoder process. A clean FFmpeg process exit
does not guarantee that its paced stdout pipe has admitted the complete tail
to the output queues. The scheduler previously treated that interval as a
runtime mismatch and could restart the same owned ad.

The runtime now reports `producer_finalizing` separately. It requires a clean
decoder exit, the current process and generation, a live unstopped pipe, and
recent PCM progress or an in-flight output admission. The existing owned-URI
drain guards wait during this interval. They still recover failed, stopped,
stale and superseded pipes. No item is completed from this signal: clean EOF
still requires admission and complete FIFO drain for every configured output.

The regression cases include a real pipe thread held after a clean decoder
exit, then released into a retained output FIFO. Ownership waits first for
the pipe and then for the FIFO; completion occurs only after full drain.
They also cover failed/stale/superseded pipes, output backpressure, URI
matching, and an ad that must neither restart nor complete during finalization.

These tests prove the local ownership logic. They do not certify listener
delivery or uninterrupted broadcasts. The independent 24-hour observer keeps
its existing failures. Energize's primary output still has no decoded audio
in the latest listener checks and is not reported as verified.
