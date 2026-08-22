#!/usr/bin/env bash
# amd64-mint profile — thin wrapper over lib/env.sh with unified pcc-data default.
set -uo pipefail
APP_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
# Unified single path first, legacy fallback second — negotiated via /mnt/pcc-data
if [ -d /mnt/pcc-data ]; then
  export PC_DATA_DIR="${PC_DATA_DIR:-/mnt/pcc-data/panamacompra-unified-records/data}"
  export PC_RECORDS_DIR="${PC_RECORDS_DIR:-/mnt/pcc-data/panamacompra-unified-records/records}"
elif [ -d /mnt/pcc-data-hp15 ]; then
  export PC_DATA_DIR="${PC_DATA_DIR:-/mnt/pcc-data-hp15/panamacompra-unified-records/data}"
  export PC_RECORDS_DIR="${PC_RECORDS_DIR:-/mnt/pcc-data-hp15/panamacompra-unified-records/records}"
fi
export PC_DEVICE_ID="${PC_DEVICE_ID:-hp23g201la}"
source "$APP_ROOT/lib/env.sh"
