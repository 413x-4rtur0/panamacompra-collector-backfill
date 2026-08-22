# Scrum for panamacompra-collector-backfill

> 2-week sprints, two hosts (hp-23 amd64, hp-15 i386-container) sharing one backlog but platform-tagged issues.

## Roles
- **PO:** prioritizes P0 `data-integrity` > P1 `backfill-progress` > P2 `platform`.
- **Dev:** implements `src/20_pipeline/037*`/`039*` logic once, deploys to both.

## Cadence
- **Planning:** pick from `BACKLOG.md` + GitHub Issues labeled `platform:amd64|i386` / `type:backfill`.
- **Daily:** `tailscale status` + `df -h /mnt/pcc-data` + `bin/pcc status` on both hosts.
- **Review:** demo backfill window (`var/closed-backfill-window.state`), `147-verify-archive.py`, monitor `monitor_servers.py` HP15 card.
- **Retro:** capture `seccomp`/`sshfs` drift (like the `i386` container fix).

## Branching
- `main` ← `develop` ← `agent/<ticket>`; `sprint/YY-WW` tags; PR needs `compileall + bash -n + docker compose config` evidence.
