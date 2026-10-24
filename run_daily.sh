#!/bin/bash
# Daily Reading Radar run. Point your scheduler at this file.
#
# Scheduling: the simplest route is GitHub Actions (see
# .github/workflows/daily.yml), which needs no machine of your own awake.
# Locally on macOS, prefer launchd over cron: launchd runs a missed job when
# the Mac wakes, cron silently skips any run where the laptop was asleep.
#
# Logs append to run.log in this folder (gitignored).
#
# PATH: launchd gives a minimal environment, so python3 is referenced without
# relying on a shell profile. /usr/bin/python3 is on the default PATH.
set -euo pipefail
cd "$(dirname "$0")"

PY=${PYTHON:-python3}

echo "=== run $(date) ==="

# Wait for the network. The reason launchd was chosen is that it runs a missed
# job on wake — but at that moment Wi-Fi is usually still associating. A run
# that starts a few seconds early fetches nothing from any source and writes a
# silently empty digest, which looks identical to "no news today".
# Bounded at ~2.5 minutes, then proceeds regardless rather than skipping a day.
for attempt in $(seq 1 30); do
  if /usr/bin/curl -sf -m 5 -o /dev/null https://connectivitycheck.gstatic.com/generate_204; then
    [ "$attempt" -gt 1 ] && echo "network up after $attempt attempt(s)"
    break
  fi
  if [ "$attempt" -eq 30 ]; then
    echo "WARNING: no network after 30 attempts — running anyway, expect a thin digest"
  else
    sleep 5
  fi
done

# Stage 1: fetch every source, score against interests.json, dedupe, write items.json.
"$PY" radar.py

# Stage 2: render digest.html + digest.md. Email only if .env is present and filled in;
# without it the digest is still written to disk, which is the fallback worth having.
if [ -f .env ]; then
  "$PY" digest.py --email
else
  "$PY" digest.py
fi

echo "=== done $(date) ==="
