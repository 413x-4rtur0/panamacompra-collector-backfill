#!/bin/bash
# Standalone 2025 Closed+Cancelled backfill for this dedicated container.
# No other pipeline runs on this machine, so unlike the original server's
# priority chain there is nothing to defer to -- just index each group to
# completion, then drain every pending detail/cotizacion.
set -uo pipefail
cd /app
export PC_CLOSED_BACKFILL_START_DATE=2025-01-01
export PC_CLOSED_BACKFILL_END_DATE=2025-12-31
export PC_CLOSED_BACKFILL_PAGES=999999
PY=/app/.venv/bin/python
DB=/app/var/data/panamacompra_archive.db

for GROUP in Closed; do
  export PC_CLOSED_GROUP="$GROUP"
  echo "=== [$(date -Iseconds)] Starting $GROUP backfill index ==="
  while true; do
    OUT=$(PC_CLOSED_MODE=backfill "$PY" src/20_pipeline/037-collect-closed-index.py 2>&1)
    echo "$OUT"
    if echo "$OUT" | grep -q "already reached the last\|marking complete"; then
      echo "=== [$(date -Iseconds)] $GROUP backfill index complete ==="
      break
    fi
    sleep 2
  done
done
unset PC_CLOSED_GROUP

echo "=== [$(date -Iseconds)] Draining details + cotizaciones ==="
while true; do
  "$PY" src/20_pipeline/037b-collect-closed-details.py 2>&1
  "$PY" src/20_pipeline/038-collect-cotizaciones.py 2>&1
  PENDING=$(sqlite3 "$DB" "SELECT COUNT(*) FROM opportunities WHERE grupo = char(67,108,111,115,101,100) AND detail_status = char(112,101,110,100,105,110,103)")
  echo "[$(date -Iseconds)] Pending: $PENDING"
  if [ "$PENDING" -eq 0 ]; then
    echo "=== [$(date -Iseconds)] All 2025 Closed+Cancelled details drained ==="
    break
  fi
  sleep 2
done
echo "=== [$(date -Iseconds)] 2025 BACKFILL COMPLETE ==="
