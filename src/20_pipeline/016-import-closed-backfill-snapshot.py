#!/usr/bin/env python3
"""Import only the dedicated Cerradas/Canceladas backfill snapshot.

This is a thin specialization of 015-import-index-snapshot.py. The separate
marker and expected groups are intentional: the regular importer must never
select a historical Closed snapshot, and this importer must never select the
Abiertas/Programadas or three-page novelty watch.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


PIPELINE_DIR = Path(__file__).resolve().parent
SOURCE = PIPELINE_DIR / "015-import-index-snapshot.py"
spec = importlib.util.spec_from_file_location("pc_index_snapshot_backfill", SOURCE)
if spec is None or spec.loader is None:
    raise RuntimeError(f"cannot load snapshot importer: {SOURCE}")
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)

module.SNAPSHOT_MARKER = "PANAMACOMPRA_MONITOR_CLOSED_BACKFILL"
module.ESTADO_TO_GROUP = (("cerrad", "Closed"),)
module.EXPECTED_GROUPS = ("Closed",)


if __name__ == "__main__":
    raise SystemExit(module.main())
