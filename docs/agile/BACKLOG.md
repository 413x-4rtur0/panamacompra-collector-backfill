# Backlog — pcc-backfill

| ID | Title | Pri | Platform | Status |
|----|-------|-----|----------|--------|
| BF-001 | Migrate `/mnt/pcc-data` → `/mnt/pcc-data` unified, keep fallback | P0 | both | To Do |
| BF-002 | Verify 2025 weekly backfill resume (`042-collect-closed-weekly-snapshot.py`) on i386 container | P0 | i386 | In Progress |
| BF-003 | `147-verify-archive.py` nightly + `200-merge-device-archives.py` idempotence | P1 | both | To Do |
| BF-004 | `arm64` stub → real deploy (Pi5) | P2 | arm64 | Backlog |
| BF-005 | Scrum board + `src/50_tools/160-manage-cron-schedule.py` sprint hooks | P1 | both | Done |

## Product Backlog (platform matrix)
- `deploy/platforms/i386-debian-amd64-container/run_2025_backfill.sh:1` — weekly index-first, detail drain loop.
- `deploy/platforms/amd64-mint/env.sh:1` — unified `PC_DATA_DIR` wrapper.
- `lib/env.sh` fallback: `/mnt/pcc-data` → `/mnt/pcc-data` → `var/data`.

## Done criteria
- No `records/` tar, `rsync` dry-run clean, `PRAGMA integrity_check` ok, `pcc health` ok on both hosts.
