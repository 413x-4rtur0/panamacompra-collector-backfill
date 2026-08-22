#!/usr/bin/env bash
set -euo pipefail
# Migrate unified storage from legacy /mnt/pcc-data-hp15 to /mnt/pcc-data (no compression)
# Usage: ./scripts/migrate-to-pcc-data.sh [--dry-run]
DRY=""
[ "${1:-}" = "--dry-run" ] && DRY="echo [dry-run]"
SRC="/mnt/pcc-data-hp15/panamacompra-unified-records"
DST="/mnt/pcc-data/panamacompra-unified-records"
if [ ! -d "$SRC" ]; then echo "Source $SRC missing — nothing to migrate"; exit 0; fi
if [ ! -d /mnt/pcc-data ]; then echo "Destination /mnt/pcc-data not mounted — mount first (sshfs //media/.../PCC-DATA /mnt/pcc-data)"; exit 2; fi
$DRY mkdir -p "$DST"
$DRY rsync -av --progress "$SRC"/ "$DST"/
echo "Migrate done. Verify with: diff <(ls -R $SRC | sort) <(ls -R $DST | sort) | head"
echo "Then update /etc/fstab to mount at /mnt/pcc-data and remove legacy symlink."
