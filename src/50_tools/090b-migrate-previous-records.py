#!/usr/bin/env python3
import csv
import json
import re
import shutil
import sqlite3
import sys
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import common as pc_common

BASE_DIR = pc_common.APP_ROOT
RECORDS_DIR = pc_common.RECORDS_DIR
DATA_DIR = pc_common.DATA_DIR
LOG_DIR = pc_common.LOG_DIR
DB_PATH = pc_common.DB_PATH
CSV_PATH = pc_common.CSV_PATH

INDEX_HEADER = [
    "numero", "grupo", "tipo_url", "estado", "descripcion", "short_description",
    "entidad", "dependencia", "fecha", "modalidad", "link", "first_seen",
    "last_seen", "date_folder", "record_folder", "index_json_path",
    "detail_status", "finish_date_guess"
]

def now_iso():
    return datetime.now().isoformat(timespec="seconds")

def clean(text):
    return re.sub(r"\s+", " ", text or "").strip()

def safe_name(text):
    text = clean(text)
    text = re.sub(r"[^A-Za-z0-9._-]+", "_", text)
    return text[:160] or "unknown"

def short_description(text, max_len=80):
    text = clean(text)
    text = re.sub(r"[^A-Za-z0-9ÁÉÍÓÚÜÑáéíóúüñ .,;:_()/-]+", "", text)
    return text[:max_len].strip()

def detect_url_type(link):
    link = link or ""
    if "/solicitud-de-cotizacion/" in link:
        return "solicitud-de-cotizacion"
    if "/pliego-de-cargos/" in link:
        return "pliego-de-cargos"
    return "unknown"

def parse_json(path):
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        return {}

def write_json_once(path, data):
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return True

def copy_once(src, dst):
    if not src.exists():
        return False
    if dst.exists():
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    return True

def derive_date_folder(old_folder, row, metadata):
    candidates = [
        row.get("first_seen"),
        row.get("last_seen"),
        metadata.get("saved_at"),
        metadata.get("first_seen"),
        metadata.get("last_seen"),
    ]

    for value in candidates:
        if not value:
            continue

        value = str(value)

        # Example: 2026-06-17T10:30:00 → 26-06-17
        m = re.search(r"(20\d{2})-(\d{2})-(\d{2})", value)
        if m:
            return f"{m.group(1)[2:]}-{m.group(2)}-{m.group(3)}"

        # Example: 17-06-2026 → 26-06-17
        m = re.search(r"(\d{2})-(\d{2})-(20\d{2})", value)
        if m:
            return f"{m.group(3)[2:]}-{m.group(2)}-{m.group(1)}"

    # Fallback: use folder modified date
    ts = old_folder.stat().st_mtime
    return datetime.fromtimestamp(ts).strftime("%y-%m-%d")

def guess_finish_date_from_text(text):
    text = text or ""

    patterns = [
        r"Fecha\s+de\s+presentaci[oó]n\s*:?\s*([0-9]{1,2}[-/][0-9]{1,2}[-/][0-9]{4}(?:\s+[0-9]{1,2}:[0-9]{2}\s*(?:AM|PM|a\.?\s*m\.?|p\.?\s*m\.?)?)?)",
        r"Fecha\s+y\s+hora\s+de\s+cierre\s*:?\s*([0-9]{1,2}[-/][0-9]{1,2}[-/][0-9]{4}(?:\s+[0-9]{1,2}:[0-9]{2}\s*(?:AM|PM|a\.?\s*m\.?|p\.?\s*m\.?)?)?)",
        r"Fecha\s+l[ií]mite\s*:?\s*([0-9]{1,2}[-/][0-9]{1,2}[-/][0-9]{4}(?:\s+[0-9]{1,2}:[0-9]{2}\s*(?:AM|PM|a\.?\s*m\.?|p\.?\s*m\.?)?)?)",
        r"Presentaci[oó]n\s+de\s+propuestas\s*:?\s*([0-9]{1,2}[-/][0-9]{1,2}[-/][0-9]{4}(?:\s+[0-9]{1,2}:[0-9]{2}\s*(?:AM|PM|a\.?\s*m\.?|p\.?\s*m\.?)?)?)",
        r"Cierre\s*:?\s*([0-9]{1,2}[-/][0-9]{1,2}[-/][0-9]{4}(?:\s+[0-9]{1,2}:[0-9]{2}\s*(?:AM|PM|a\.?\s*m\.?|p\.?\s*m\.?)?)?)",
    ]

    for pat in patterns:
        m = re.search(pat, text, flags=re.IGNORECASE)
        if m:
            return clean(m.group(1))

    return ""

def ensure_csv():
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    if not CSV_PATH.exists():
        with CSV_PATH.open("w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(INDEX_HEADER)

def init_db():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")

    conn.execute("""
    CREATE TABLE IF NOT EXISTS opportunities (
        numero TEXT PRIMARY KEY,
        grupo TEXT,
        tipo_url TEXT,
        estado TEXT,
        descripcion TEXT,
        short_description TEXT,
        entidad TEXT,
        dependencia TEXT,
        fecha TEXT,
        modalidad TEXT,
        link TEXT,
        first_seen TEXT,
        last_seen TEXT,
        date_folder TEXT,
        record_folder TEXT,
        index_json_path TEXT,
        detail_status TEXT DEFAULT 'pending',
        detail_attempts INTEGER DEFAULT 0,
        detail_saved_at TEXT,
        detail_json_path TEXT,
        finish_date_guess TEXT
    )
    """)

    conn.commit()
    ensure_csv()
    return conn

def db_row_exists(conn, numero):
    return conn.execute("SELECT numero FROM opportunities WHERE numero = ?", (numero,)).fetchone() is not None

def append_csv(row):
    with CSV_PATH.open("a", newline="", encoding="utf-8") as f:
        csv.writer(f).writerow([
            row["numero"],
            row["grupo"],
            row["tipo_url"],
            row["estado"],
            row["descripcion"],
            row["short_description"],
            row["entidad"],
            row["dependencia"],
            row["fecha"],
            row["modalidad"],
            row["link"],
            row["first_seen"],
            row["last_seen"],
            row["date_folder"],
            row["record_folder"],
            row["index_json_path"],
            row["detail_status"],
            row["finish_date_guess"],
        ])

def insert_or_update_db(conn, row):
    if db_row_exists(conn, row["numero"]):
        conn.execute("""
        UPDATE opportunities
        SET grupo = ?,
            tipo_url = ?,
            estado = ?,
            descripcion = ?,
            short_description = ?,
            entidad = ?,
            dependencia = ?,
            fecha = ?,
            modalidad = ?,
            link = ?,
            last_seen = ?,
            date_folder = ?,
            record_folder = ?,
            index_json_path = ?,
            detail_status = ?,
            detail_saved_at = ?,
            detail_json_path = ?,
            finish_date_guess = ?
        WHERE numero = ?
        """, (
            row["grupo"],
            row["tipo_url"],
            row["estado"],
            row["descripcion"],
            row["short_description"],
            row["entidad"],
            row["dependencia"],
            row["fecha"],
            row["modalidad"],
            row["link"],
            row["last_seen"],
            row["date_folder"],
            row["record_folder"],
            row["index_json_path"],
            row["detail_status"],
            row["detail_saved_at"],
            row["detail_json_path"],
            row["finish_date_guess"],
            row["numero"],
        ))
        conn.commit()
        return "updated"

    conn.execute("""
    INSERT INTO opportunities (
        numero, grupo, tipo_url, estado, descripcion, short_description,
        entidad, dependencia, fecha, modalidad, link, first_seen, last_seen,
        date_folder, record_folder, index_json_path, detail_status,
        detail_attempts, detail_saved_at, detail_json_path, finish_date_guess
    )
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        row["numero"],
        row["grupo"],
        row["tipo_url"],
        row["estado"],
        row["descripcion"],
        row["short_description"],
        row["entidad"],
        row["dependencia"],
        row["fecha"],
        row["modalidad"],
        row["link"],
        row["first_seen"],
        row["last_seen"],
        row["date_folder"],
        row["record_folder"],
        row["index_json_path"],
        row["detail_status"],
        row["detail_attempts"],
        row["detail_saved_at"],
        row["detail_json_path"],
        row["finish_date_guess"],
    ))

    conn.commit()
    append_csv(row)
    return "inserted"

def is_old_flat_record_folder(path):
    if not path.is_dir():
        return False

    # Skip new date folders like 26-06-17
    if re.fullmatch(r"\d{2}-\d{2}-\d{2}", path.name):
        return False

    return (
        (path / "row.json").exists()
        or (path / "detail.html").exists()
        or (path / "detail.txt").exists()
        or (path / "metadata.json").exists()
    )

def migrate_one(conn, old_folder):
    row_json = old_folder / "row.json"
    metadata_json = old_folder / "metadata.json"
    old_detail_html = old_folder / "detail.html"
    old_detail_txt = old_folder / "detail.txt"

    row = parse_json(row_json)
    metadata = parse_json(metadata_json)

    numero = clean(row.get("numero") or metadata.get("numero") or old_folder.name)

    if not numero:
        return {"status": "skipped", "reason": "missing numero", "old": str(old_folder)}

    date_folder = derive_date_folder(old_folder, row, metadata)
    new_folder = RECORDS_DIR / date_folder / safe_name(numero)
    new_folder.mkdir(parents=True, exist_ok=True)

    new_index_json = new_folder / f"{safe_name(numero)}.json"
    new_detail_json = new_folder / f"{safe_name(numero)}.detail.json"
    new_detail_html = new_folder / f"{safe_name(numero)}.detail.html"
    new_detail_txt = new_folder / f"{safe_name(numero)}.detail.txt"

    link = clean(row.get("link") or metadata.get("link") or "")
    descripcion = clean(row.get("descripcion") or row.get("description") or "")
    grupo = clean(row.get("grupo") or row.get("status_group") or "")
    estado = clean(row.get("estado") or "")
    entidad = clean(row.get("entidad") or "")
    dependencia = clean(row.get("dependencia") or "")
    fecha = clean(row.get("fecha") or "")
    modalidad = clean(row.get("modalidad") or "")

    # If group is missing, infer roughly from Estado.
    if not grupo:
        if "program" in estado.lower():
            grupo = "Programadas"
        elif "abiert" in estado.lower():
            grupo = "Abiertas"
        else:
            grupo = "Unknown"

    first_seen = clean(row.get("first_seen") or metadata.get("saved_at") or now_iso())
    last_seen = clean(row.get("last_seen") or first_seen)

    detail_text = ""
    if old_detail_txt.exists():
        detail_text = old_detail_txt.read_text(encoding="utf-8", errors="ignore")

    finish_date_guess = clean(row.get("finish_date_guess") or metadata.get("finish_date_guess") or guess_finish_date_from_text(detail_text))

    detail_html_copied = copy_once(old_detail_html, new_detail_html)
    detail_txt_copied = copy_once(old_detail_txt, new_detail_txt)

    # Copy any existing tables folder from old structure if present.
    old_tables = old_folder / "tables"
    new_tables = new_folder / "tables"
    tables_copied = 0
    if old_tables.exists() and old_tables.is_dir():
        new_tables.mkdir(parents=True, exist_ok=True)
        for src in old_tables.glob("*"):
            if src.is_file():
                dst = new_tables / src.name
                if copy_once(src, dst):
                    tables_copied += 1

    index_data = {
        "numero": numero,
        "grupo": grupo,
        "tipo_url": detect_url_type(link),
        "estado": estado,
        "descripcion": descripcion,
        "short_description": short_description(descripcion),
        "entidad": entidad,
        "dependencia": dependencia,
        "fecha": fecha,
        "modalidad": modalidad,
        "link": link,
        "first_seen": first_seen,
        "last_seen": last_seen,
        "date_folder": date_folder,
        "record_folder": str(new_folder),
        "index_json_path": str(new_index_json),
        "detail_status": "saved" if new_detail_html.exists() and new_detail_txt.exists() else "pending",
        "finish_date_guess": finish_date_guess,
        "migrated_from": str(old_folder),
        "migrated_at": now_iso(),
    }

    index_written = write_json_once(new_index_json, index_data)

    detail_data = {
        "numero": numero,
        "grupo": grupo,
        "tipo_url": detect_url_type(link),
        "link": link,
        "source": "PanamaCompra",
        "saved_at": metadata.get("saved_at") or now_iso(),
        "finish_date_guess": finish_date_guess,
        "short_description": short_description(descripcion),
        "descripcion_index": descripcion,
        "entidad_index": entidad,
        "dependencia_index": dependencia,
        "fecha_index": fecha,
        "modalidad_index": modalidad,
        "migrated_from": str(old_folder),
        "files": {
            "index_json": str(new_index_json),
            "detail_json": str(new_detail_json),
            "detail_html": str(new_detail_html),
            "detail_txt": str(new_detail_txt),
            "tables_folder": str(new_tables),
        }
    }

    detail_json_written = False
    if new_detail_html.exists() or new_detail_txt.exists():
        detail_json_written = write_json_once(new_detail_json, detail_data)

    db_row = {
        "numero": numero,
        "grupo": grupo,
        "tipo_url": detect_url_type(link),
        "estado": estado,
        "descripcion": descripcion,
        "short_description": short_description(descripcion),
        "entidad": entidad,
        "dependencia": dependencia,
        "fecha": fecha,
        "modalidad": modalidad,
        "link": link,
        "first_seen": first_seen,
        "last_seen": last_seen,
        "date_folder": date_folder,
        "record_folder": str(new_folder),
        "index_json_path": str(new_index_json),
        "detail_status": "saved" if new_detail_html.exists() and new_detail_txt.exists() else "pending",
        "detail_attempts": 0,
        "detail_saved_at": metadata.get("saved_at") or now_iso() if new_detail_html.exists() and new_detail_txt.exists() else "",
        "detail_json_path": str(new_detail_json) if new_detail_json.exists() else "",
        "finish_date_guess": finish_date_guess,
    }

    db_result = insert_or_update_db(conn, db_row)

    return {
        "status": "migrated",
        "numero": numero,
        "old_folder": str(old_folder),
        "new_folder": str(new_folder),
        "date_folder": date_folder,
        "index_written": index_written,
        "detail_json_written": detail_json_written,
        "detail_html_copied": detail_html_copied,
        "detail_txt_copied": detail_txt_copied,
        "tables_copied": tables_copied,
        "db_result": db_result,
        "finish_date_guess": finish_date_guess,
    }

def main():
    if not RECORDS_DIR.exists():
        print(f"Records folder not found: {RECORDS_DIR}")
        return

    conn = init_db()

    candidates = sorted([p for p in RECORDS_DIR.iterdir() if is_old_flat_record_folder(p)])

    migrated = 0
    skipped = 0
    inserted = 0
    updated = 0

    print(f"Migration started: {now_iso()}")
    print(f"Old flat record folders found: {len(candidates)}")
    print("")

    results = []

    for folder in candidates:
        result = migrate_one(conn, folder)
        results.append(result)

        if result["status"] == "migrated":
            migrated += 1
            if result.get("db_result") == "inserted":
                inserted += 1
            elif result.get("db_result") == "updated":
                updated += 1

            print(f"MIGRATED: {result['numero']}")
            print(f"  from: {result['old_folder']}")
            print(f"  to:   {result['new_folder']}")
            if result.get("finish_date_guess"):
                print(f"  finish_date_guess: {result['finish_date_guess']}")
        else:
            skipped += 1
            print(f"SKIPPED: {result.get('old')} | {result.get('reason')}")

    db_total = conn.execute("SELECT COUNT(*) AS c FROM opportunities").fetchone()["c"]
    pending = conn.execute("SELECT COUNT(*) AS c FROM opportunities WHERE detail_status != 'saved'").fetchone()["c"]

    summary = {
        "finished_at": now_iso(),
        "old_flat_record_folders_found": len(candidates),
        "migrated": migrated,
        "skipped": skipped,
        "db_inserted": inserted,
        "db_updated": updated,
        "db_total_records": db_total,
        "db_pending_details": pending,
        "records_dir": str(RECORDS_DIR),
        "db_path": str(DB_PATH),
    }

    summary_path = LOG_DIR / f"migrate_previous_records_summary_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    summary_path.write_text(json.dumps({"summary": summary, "results": results}, ensure_ascii=False, indent=2), encoding="utf-8")

    print("")
    print("Migration summary")
    print("-----------------")
    for k, v in summary.items():
        print(f"{k}: {v}")
    print(f"summary_json: {summary_path}")

if __name__ == "__main__":
    main()
