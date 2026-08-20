# RadioTEDU OnAir Broadcast Configuration & Reliability Runbook

**Date Updated:** 2026-08-16
**Status:** Certified Operational

---

## 1. Credentials & Authentication

- **Icecast / TinyIce Source User:** `source`
- **Icecast / TinyIce Source Password:** Resolve the protected value from the credential vault; never record it in source or documentation.
  *(Decrypted via Windows DPAPI `CryptUnprotectData` from `C:\Users\tedu\AppData\Local\RadioTEDU\OnAir\secrets\station-credentials.json`)*
- **Admin Operator Username:** `admin`
- **Admin Operator Password:** Resolve it from the protected operator credential store; never record it here.

---

## 2. Station Mount Mappings

The following exact mount names are configured in `cleanroom.db` (`station_outputs` table, `icecast_mount` column):

| Station ID | Station Name | Configured Mount | Icecast Host / Port |
|---|---|---|---|
| **1** | RadioTEDU Classical | `/classic` | `stream.radiotedu.com:11154` / `10.98.98.75:11154` |
| **2** | RadioTEDU Lo-Fi | `/lofi` | `stream.radiotedu.com:11154` |
| **4** | RadioTEDU Pop | `/radio` | `stream.radiotedu.com:11154` |
| **5** | RadioTEDU Jazz | `/cazz` | `stream.radiotedu.com:11154` |
| **8** | RadioTEDU Rock | `/rock` | `stream.radiotedu.com:11154` |
| **9** | RadioTEDU Energize | `/energize` | `stream.radiotedu.com:11154` |

---

## 3. Database Locations & Autostart

The changes have been permanently saved in SQLite databases with `commit`:

1. **Production System Database:** `C:\ProgramData\RadioTEDU\OnAir\cleanroom.db`
2. **RadioTEDU OnAir (New) Database:** `C:\Users\tedu\Documents\RadioTEDU-OnAir-Radio\run\new-program\data\cleanroom.db`

### Permanent Automatic Startup:
`station_settings` contains `broadcast_autostart_enabled = 'true'` for the six protected stations.
Whenever the backend starts up, stations automatically begin broadcasting without requiring operator button clicks.

---

## 4. How to Launch & Restart

- **RadioTEDU OnAir (New) Launcher:**
  `powershell -NoProfile -ExecutionPolicy Bypass -File "C:\Users\tedu\Documents\RadioTEDU-OnAir-Radio\tools\launch_new_program.ps1"`
- **Windows Service:** `RadioTEDU.OnAir.Supervisor`
- **Web Interface:** `http://127.0.0.1:18110/?station_id=1#onair`

---

## 5. Technical Cause Analysis: Broadcast Micro-Drops (Micro-Pauses)

Without changing code, here are the exact architectural reasons why some broadcasts may drop for a few milliseconds:

1. **External Drive Disk Read I/O Latency:**
   Audio tracks are played directly from external hard drives (`H:\Broadcast\...` & `H:\RadioTEDU Song Database Overflow\...`). Disk spin-up delays or file handle open latencies when switching tracks produce 10–50ms audio buffer delays.

2. **FFmpeg On-the-Fly Audio Resampling Gaps:**
   When transitioning between audio files recorded at different sample rates (e.g., 44.1 kHz MP3 vs 48.0 kHz FLAC/M4A), FFmpeg dynamic audio filter graph re-initializes, causing a micro-gap during crossfades.

3. **TCP Socket Network Jitter to TinyIce Server (`10.98.98.75`):**
   The playout backend streams live PCM chunks over local network TCP (`10.10.1.200` -> `10.98.98.75:11154`). Occasional TCP packet retransmissions or small socket send buffer capacity create momentary underruns on TinyIce.

4. **Nginx Web Server Proxy Buffering:**
   `stream.radiotedu.com` runs Nginx on ports 80/443. Nginx buffers incoming audio stream chunks before delivering them to web browsers. If client player buffer size is tiny, micro-pauses occur during chunk flushes.
