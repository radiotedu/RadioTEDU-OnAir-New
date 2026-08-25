#!/bin/bash
set -euo pipefail
/bin/launchctl kickstart -k "gui/$UID/com.radiotedu.onair"
sleep 3
/usr/bin/open "http://127.0.0.1:18110/?station_id=1#onair"
