# amd64-mint (hp-23-g201la-l, native)

> Native 64-bit Linux Mint — Playwright Firefox runs directly on host, no container needed.

- **Host:** `hp-23-g201la-l` `AMD E2-6110`, `Mint 22.3`, `3.3G RAM`, `enp1s0 192.168.10.20`.
- **Install:** `./setup.sh` (`PC_SETUP_SKIP_DOCKER=0`) → `.venv` + `playwright install firefox` + `docker compose up -d`.
- **Data:** `/mnt/pcc-data/panamacompra-unified-records` (primary) fallback `/mnt/pcc-data-hp15` (legacy sshfs from hp-15). `var/data`/`var/records` symlinks created by `lib/env.sh`.
- **Run:** `bin/pcc start` or `src/20_pipeline/039-run-closed-backfill.sh` (weekly windows).
