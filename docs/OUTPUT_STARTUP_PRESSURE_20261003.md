# Bound startup spread and avoid disconnecting a flowing source

Live source telemetry from 01:48:39–02:03:39 UTC on 3 October shows a new
worker's primary output being forcibly recovered about one second after
startup. The sink's deliberately delayed connector was still starting. The
configured mount spread could delay each quality output independently by up
to 30 seconds. Radio-low then accumulated a full FIFO and reconnected
approximately every 33 seconds despite fresh encoder input and network writes.

At 01:50:07 the primary queue was empty while Radio-low retained about 20
seconds. The existing full-output EOF gate held ownership until the low
queue drained at 01:50:27. Primary silence counters increased during this
wait. This is measured source-generated silence, not a claim based only on
natural quiet passages in a song.

The runtime now uses at most one second of initial mount spread. Each sink
reports its initial connector age and bounded deadline. Recovery waits only
while the first connector and PCM writer remain live, no transport failure is
known, no body bytes have yet been sent, and that explicit deadline has not
expired. Startup remains unverified, rather than being reported healthy.

A sustained full queue stays visible in telemetry. It no longer forces a
disconnect when the source has a final positive acknowledgement, a live
encoder and both writers, positive encoded bytes and fresh PCM/network
activity. Failed writers, stale writes, unconfirmed sources and lost PCM do
not qualify. Source transport health also rejects an explicitly unconfirmed
acknowledgement. None of these signals certify listener delivery.

The five newly reproduced failures passed after the change. Twenty-five
focused startup/pressure cases and the broader 294-case regression suite
passed. The latter includes advertisement ownership, EOF/FIFO gates,
prepared decoders, quality backpressure, Icecast transport/recovery, leases,
durable transaction boundaries and worker health.

Codecs, bitrates, output count, retained PCM and clean-EOF completion rules
are unchanged. Energize's primary listener still receives no PCM; this
change does not certify or solve that separate origin-side delivery failure.
The independent 24-hour observer retains prior failures. Production
validation is required before claiming uninterrupted delivery.
