# Platforms — panamacompra-collector-backfill

> Backfill runs on **two hosts** sharing one codebase but different arches. This matrix keeps collector logic identical, only the *deploy* differs.

| Platform | Host | Arch userspace | How it runs | Data mount |
|----------|------|----------------|-------------|------------|
| `amd64-mint` | hp-23-g201la-l (192.168.10.20) | x86_64 | Native (`./setup.sh`, Firefox host) | `/mnt/pcc-data` primary, `/mnt/pcc-data-hp15` legacy fallback |
| `i386-debian-amd64-container` | hp-15-bw036nr-r (192.168.10.40) | i386 (32-bit) | `docker` `debian:bookworm-slim --platform linux/amd64 --security-opt seccomp=unconfined` (Firefox inside container) | Same `/mnt/pcc-data` (local external `PCC -DATA` drive) |
| `arm64` | — | aarch64 | Stub — future | — |

## Files
- `i386-debian-amd64-container/Dockerfile.backfill`, `env.sh`, `run_2025_backfill.sh` — standalone 2025 weekly backfill loop (index-first, detail drain), no priority lock.
- `amd64-mint/env.sh` — thin unified-pcc-data wrapper over `lib/env.sh`.
- `arm64/README.md` — placeholder.

## Unified records
- **Single path:** `/mnt/pcc-data/panamacompra-unified-records` (records + data). Both hosts mount the same external `PCC-DATA` drive (`hp-15` local, `hp-23` via `sshfs a2gutierrezmora@hp-15-bw036nr-r.local:/media/.../PCC\040-DATA /mnt/pcc-data` with `allow_other,x-systemd.automount`). Legacy `/mnt/pcc-data-hp15` kept as fallback until migration finishes.
- **No compression** of `records/` — plain folders for `rsync` + SQLite integrity (`PRAGMA integrity_check` failed on sshfs before, so DB stays local `var/db-local/` while records stay on share).

See `docs/agile/SCRUM.md` for sprint handling of platform work.
