# Plan — split pcc (pre-Cerradas) + pcc-backfill (platform matrix) — executed 2026-08-22

> Decisions: 1A = base 4d88f89^ (20aaa73 pure Abiertas/Programadas), 2 = new repo panamacompra-collector-backfill, 3 = deploy/platforms (amd64-mint, i386-debian-amd64-container, arm64) + agile/scrum both repos + unified pcc-data + no records compression + backups.

## 0. Context

- HEAD at 242cb43 deploy/hp15bw-debian-i386-portable (sole deploy commit, parent fdc4cea). Backfill root 4d88f89 → 6805157 → 064bad9 (lib/env.sh device split) → 9f8423d (monitor_servers.py) → 242cb43.
- Hosts: hp-23-g201la-l 192.168.10.20 (amd64 Mint, enp1s0) + hp-15-bw036nr-d.local 192.168.10.40 (i386, external pcc-data, fuse.sshfs /mnt/pcc-data) — hp-15 offline during split (100% loss).
- Unified intent: single /mnt/pcc-data/panamacompra-unified-records (records+data), legacy /mnt/pcc-data fallback, no tar on records.

## 1. Backups (compressed, no records)

```bash
mkdir -p ~/backups
git tag pre-split-20260822 242cb43; git push origin tag pre-split-20260822
git bundle create ~/backups/pcc-pre-split-20260822-0036-hp23.bundle --all
tar -czf ~/backups/pcc-pre-split-20260822-0036-hp23-norecords.tar.gz --exclude=.venv --exclude=.git/objects --exclude=records --exclude=records_test --exclude=var/data --exclude=var/records --exclude=var/integrations -C ~ Apps/panamacompra-collector
# post-split same for both repos (pcc 72M, backfill 2.0M)
```

## 2. New repo

```bash
gh repo create 413x-4rtur0/panamacompra-collector-backfill --public --source=. --remote=backfill --push
git push backfill --all --tags
```

## 3. Rewind pcc to 1A

```bash
git stash push -m "pre-split-stash" --include-untracked  # backup wip
git reset --hard 20aaa73  # 4d88f89^
sudo rm -rf var.pre-network-migration-20260802
git clean -fd
# now at 20aaa73, 64 files diff to 242cb43 removed
```

## 4. Platform matrix (pcc-backfill only)

```
deploy/platforms/
  i386-debian-amd64-container/ (moved hp15bw) Dockerfile.backfill, env.sh, run_2025_backfill.sh
  amd64-mint/ env.sh + README (hp-23 native)
  arm64/ README stub
  README.md matrix table
```

## 5. Unified pcc-data

- lib/env.sh: if [ -d /mnt/pcc-data ] -> PC_DATA_DIR=/mnt/pcc-data/... elif /mnt/pcc-data -> legacy.
- .env.example: PC_DATA_DIR=/mnt/pcc-data/... + comment Records NEVER compressed
- scripts/migrate-to-pcc-data.sh: rsync -av $SRC/ $DST/ helper

## 6. Agile/Scrum both repos

- docs/agile/SCRUM.md (2-week sprints, planning/review/retro, roles)
- docs/agile/BACKLOG.md (PCC-001 unified path, BF-001 migrate, etc)
- docs/AGILE_PROCESS.md already present, extend

## 7. Verify & push

```bash
python3 -m compileall -q src; bash -n lib/env.sh; bash -n deploy/platforms/*/env.sh
docker compose config  # pcc ok, backfill needs .env copy
git add .env.example lib/env.sh deploy/platforms docs/agile scripts/migrate-to-pcc-data.sh
git commit -m "split: pcc core (1A) ..." / "feat: pcc-backfill platform matrix ..."
git push origin agent/priority-run-queue --force-with-lease  # pcc
git push origin agent/priority-run-queue  # backfill
git bundle/tar post-split backups
```

## 8. Result

- pcc: branch agent/priority-run-queue 1c16e82 (20aaa73 + unified + agile stub), origin updated.
- pcc-backfill: branch a6990dd (242cb43 + platform matrix), origin new repo.
- Records: unified single path, no compression, rsync-friendly.

## 9. Next when hp-15 online

- rsync hp-15 var/data + records to /mnt/pcc-data, run scripts/migrate-to-pcc-data.sh --dry-run.
- Update /etc/fstab to mount at /mnt/pcc-data (keep fallback).

