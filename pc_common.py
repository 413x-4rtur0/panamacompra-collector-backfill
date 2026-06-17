#!/usr/bin/env python3
import csv
import json
import os
import re
import sqlite3
from pathlib import Path
from datetime import datetime

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
RECORDS_DIR = BASE_DIR / "records"
LOG_DIR = DATA_DIR / "logs"
DB_PATH = DATA_DIR / "panamacompra_archive.db"
CSV_PATH = DATA_DIR / "panamacompra_index.csv"

BASE_URL = "https://www.panamacompra.gob.pa/Inicio/#/cotizaciones-en-linea/cotizaciones-en-linea"

INDEX_HEADER = [
    "numero", "grupo", "tipo_url", "estado", "descripcion", "short_description",
    "entidad", "dependencia", "fecha", "modalidad", "link", "first_seen",
    "last_seen", "date_folder", "record_folder", "index_json_path",
    "detail_status", "finish_date_guess"
]

def now_iso():
    return datetime.now().isoformat(timespec="seconds")

def date_folder_name():
    return datetime.now().strftime("%y-%m-%d")

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

def browser_executable():
    for candidate in ["/usr/bin/chromium", "/usr/bin/google-chrome", "/usr/bin/chromium-browser"]:
        if Path(candidate).exists():
            return candidate
    raise RuntimeError("No Chromium/Chrome executable found.")

def ensure_dirs():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    RECORDS_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)

def init_db():
    ensure_dirs()

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

    if not CSV_PATH.exists():
        with CSV_PATH.open("w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(INDEX_HEADER)

    return conn

def get_record_folder(date_folder, numero):
    return RECORDS_DIR / date_folder / safe_name(numero)

def archive_index_json_path(record_folder, numero):
    return record_folder / f"{safe_name(numero)}.json"

def archive_detail_json_path(record_folder, numero):
    return record_folder / f"{safe_name(numero)}.detail.json"

def archive_complete(record_folder, numero):
    n = safe_name(numero)
    return (
        record_folder.exists()
        and (record_folder / f"{n}.json").exists()
        and (record_folder / f"{n}.detail.json").exists()
        and (record_folder / f"{n}.detail.html").exists()
        and (record_folder / f"{n}.detail.txt").exists()
    )

def write_json_once(path, data):
    if path.exists():
        return False
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return True

def write_text_once(path, text):
    if path.exists():
        return False
    path.write_text(text, encoding="utf-8", errors="ignore")
    return True

def find_existing_opportunity(conn, numero):
    return conn.execute("SELECT * FROM opportunities WHERE numero = ?", (numero,)).fetchone()

def append_index_csv(row):
    # Append-only "first seen" log: written once when a NUMERO is first inserted
    # and never updated afterwards. It does NOT reflect later changes to
    # last_seen or detail_status. Use the SQLite DB for current state.
    with CSV_PATH.open("a", newline="", encoding="utf-8") as f:
        csv.writer(f).writerow([
            row.get("numero"),
            row.get("grupo"),
            row.get("tipo_url"),
            row.get("estado"),
            row.get("descripcion"),
            row.get("short_description"),
            row.get("entidad"),
            row.get("dependencia"),
            row.get("fecha"),
            row.get("modalidad"),
            row.get("link"),
            row.get("first_seen"),
            row.get("last_seen"),
            row.get("date_folder"),
            row.get("record_folder"),
            row.get("index_json_path"),
            row.get("detail_status"),
            row.get("finish_date_guess"),
        ])

def insert_or_update_index(conn, row):
    existing = find_existing_opportunity(conn, row["numero"])

    if existing:
        # Do not rewrite files. Only update lightweight DB tracking.
        conn.execute("""
        UPDATE opportunities
        SET grupo = ?,
            estado = ?,
            descripcion = ?,
            short_description = ?,
            entidad = ?,
            dependencia = ?,
            fecha = ?,
            modalidad = ?,
            link = ?,
            last_seen = ?,
            tipo_url = ?
        WHERE numero = ?
        """, (
            row["grupo"],
            row["estado"],
            row["descripcion"],
            row["short_description"],
            row["entidad"],
            row["dependencia"],
            row["fecha"],
            row["modalidad"],
            row["link"],
            row["last_seen"],
            row["tipo_url"],
            row["numero"],
        ))
        conn.commit()
        return "existing"

    conn.execute("""
    INSERT INTO opportunities (
        numero, grupo, tipo_url, estado, descripcion, short_description,
        entidad, dependencia, fecha, modalidad, link, first_seen, last_seen,
        date_folder, record_folder, index_json_path, detail_status,
        finish_date_guess
    )
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
        row["finish_date_guess"],
    ))

    conn.commit()
    append_index_csv(row)
    return "new"

def guess_finish_date_from_text(text):
    text = text or ""
    patterns = [
        r"Fecha\s+de\s+presentaci[oó]n\s*:?\s*([0-9]{1,2}[-/][0-9]{1,2}[-/][0-9]{4}(?:\s+[0-9]{1,2}:[0-9]{2}\s*(?:AM|PM|a\.?\s*m\.?|p\.?\s*m\.?)?)?)",
        r"Fecha\s+y\s+hora\s+de\s+cierre\s*:?\s*([0-9]{1,2}[-/][0-9]{1,2}[-/][0-9]{4}(?:\s+[0-9]{1,2}:[0-9]{2}\s*(?:AM|PM|a\.?\s*m\.?|p\.?\s*m\.?)?)?)",
        r"Fecha\s+l[ií]mite\s*:?\s*([0-9]{1,2}[-/][0-9]{1,2}[-/][0-9]{4}(?:\s+[0-9]{1,2}:[0-9]{2}\s*(?:AM|PM|a\.?\s*m\.?|p\.?\s*m\.?)?)?)",
        r"Presentaci[oó]n\s+de\s+propuestas\s*:?\s*([0-9]{1,2}[-/][0-9]{1,2}[-/][0-9]{4}(?:\s+[0-9]{1,2}:[0-9]{2}\s*(?:AM|PM|a\.?\s*m\.?|p\.?\s*m\.?)?)?)",
    ]

    for pat in patterns:
        m = re.search(pat, text, flags=re.IGNORECASE)
        if m:
            return clean(m.group(1))

    return ""

def update_detail_status(conn, numero, status, detail_json_path=None, finish_date_guess=None):
    conn.execute("""
    UPDATE opportunities
    SET detail_status = ?,
        detail_attempts = detail_attempts + 1,
        detail_saved_at = ?,
        detail_json_path = COALESCE(?, detail_json_path),
        finish_date_guess = COALESCE(NULLIF(?, ''), finish_date_guess)
    WHERE numero = ?
    """, (
        status,
        now_iso() if status == "saved" else None,
        str(detail_json_path) if detail_json_path else None,
        finish_date_guess or "",
        numero,
    ))
    conn.commit()
