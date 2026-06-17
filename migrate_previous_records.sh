#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" || exit 1

mkdir -p data/logs

python3 migrate_previous_records.py | tee "data/logs/migrate_previous_records_$(date +%Y%m%d_%H%M%S).log"
