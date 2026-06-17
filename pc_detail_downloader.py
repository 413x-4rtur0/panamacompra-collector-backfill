#!/usr/bin/env python3
import json
import os
import re
from pathlib import Path
from playwright.sync_api import sync_playwright
from pc_common import *

DETAIL_LIMIT = int(os.environ.get("PC_DETAIL_LIMIT", "10"))
MAX_DETAIL_ATTEMPTS = int(os.environ.get("PC_MAX_DETAIL_ATTEMPTS", "5"))

def close_popup(page):
    page.evaluate("""
    (() => {
      try {
        const noMostrar = document.querySelector('#checkDefaultSQ');
        if (noMostrar && !noMostrar.checked) noMostrar.click();

        const closeBtn = document.querySelector('ngb-modal-window .btn-close');
        if (closeBtn) closeBtn.click();

        document.querySelectorAll('ngb-modal-window, ngb-modal-backdrop').forEach(el => el.remove());
        document.body.classList.remove('modal-open');
        document.body.style.overflow = '';
      } catch (e) {}
    })();
    """)

def extract_tables(page):
    return page.evaluate("""
    (() => {
      function clean(text) {
        return (text || '').replace(/\\s+/g, ' ').trim();
      }

      return Array.from(document.querySelectorAll('table')).map((table, idx) => {
        const rows = Array.from(table.querySelectorAll('tr')).map(tr =>
          Array.from(tr.querySelectorAll('th, td')).map(td => clean(td.innerText))
        ).filter(r => r.some(Boolean));

        let headers = [];
        let dataRows = rows;

        if (rows.length > 0) {
          headers = rows[0];
          dataRows = rows.slice(1);
        }

        return {
          table_index: idx + 1,
          headers,
          rows: dataRows,
          raw_rows: rows
        };
      });
    })();
    """)

def extract_label_values_from_text(text):
    labels = [
        "Número",
        "Estado",
        "Descripción",
        "Entidad",
        "Dependencia",
        "Fecha",
        "Modalidad",
        "Lugar",
        "Precio",
        "Valor",
        "Monto",
        "Fecha de presentación",
        "Fecha y hora de cierre",
        "Fecha límite",
        "Presentación de propuestas",
        "Unidad de compra",
        "Contacto",
    ]

    result = {}
    lines = [clean(x) for x in text.splitlines() if clean(x)]

    for i, line in enumerate(lines):
        for label in labels:
            if line.lower() == label.lower() and i + 1 < len(lines):
                result[label] = lines[i + 1]
                break
            elif line.lower().startswith(label.lower() + ":"):
                result[label] = clean(line.split(":", 1)[1])
                break

    return result

def detail_pending_rows(conn, limit, max_attempts):
    # Skip rows that have already failed too many times, so a permanently broken
    # URL is not retried forever and cannot starve newer rows. Rows with fewer
    # attempts are processed first.
    return conn.execute("""
    SELECT *
    FROM opportunities
    WHERE detail_status != 'saved'
      AND detail_attempts < ?
    ORDER BY detail_attempts ASC, first_seen ASC
    LIMIT ?
    """, (max_attempts, limit)).fetchall()

def save_table_jsons(record_folder, numero, tables):
    tables_dir = record_folder / "tables"
    tables_dir.mkdir(parents=True, exist_ok=True)

    written = 0
    for table in tables:
        path = tables_dir / f"{safe_name(numero)}.table_{table['table_index']:03d}.json"
        if write_json_once(path, table):
            written += 1

    return written

def process_detail(browser, conn, row):
    numero = row["numero"]
    record_folder = Path(row["record_folder"])
    record_folder.mkdir(parents=True, exist_ok=True)

    n = safe_name(numero)
    html_path = record_folder / f"{n}.detail.html"
    txt_path = record_folder / f"{n}.detail.txt"
    detail_json_path = record_folder / f"{n}.detail.json"

    if html_path.exists() and txt_path.exists() and detail_json_path.exists():
        update_detail_status(conn, numero, "saved", detail_json_path=detail_json_path)
        return "skipped_complete"

    page = browser.new_page(viewport={"width": 1280, "height": 720})

    try:
        page.goto(row["link"], wait_until="domcontentloaded", timeout=90000)
        page.wait_for_timeout(12000)
        close_popup(page)

        html = page.content()
        text = page.locator("body").inner_text(timeout=25000)
        tables = extract_tables(page)
        label_values = extract_label_values_from_text(text)
        finish_date_guess = guess_finish_date_from_text(text)

        write_text_once(html_path, html)
        write_text_once(txt_path, text)
        tables_written = save_table_jsons(record_folder, numero, tables)

        detail_data = {
            "numero": numero,
            "grupo": row["grupo"],
            "tipo_url": row["tipo_url"],
            "link": row["link"],
            "source": "PanamaCompra",
            "saved_at": now_iso(),
            "finish_date_guess": finish_date_guess,
            "short_description": row["short_description"],
            "descripcion_index": row["descripcion"],
            "entidad_index": row["entidad"],
            "dependencia_index": row["dependencia"],
            "fecha_index": row["fecha"],
            "modalidad_index": row["modalidad"],
            "label_values_detected": label_values,
            "tables_count": len(tables),
            "tables_written_now": tables_written,
            "files": {
                "index_json": row["index_json_path"],
                "detail_json": str(detail_json_path),
                "detail_html": str(html_path),
                "detail_txt": str(txt_path),
                "tables_folder": str(record_folder / "tables"),
            }
        }

        write_json_once(detail_json_path, detail_data)
        update_detail_status(conn, numero, "saved", detail_json_path=detail_json_path, finish_date_guess=finish_date_guess)

        return "saved"

    except Exception as e:
        err_path = record_folder / f"{n}.error.txt"
        write_text_once(err_path, str(e))
        update_detail_status(conn, numero, "failed")
        return "failed"

    finally:
        page.close()

def main():
    conn = init_db()
    rows = detail_pending_rows(conn, DETAIL_LIMIT, MAX_DETAIL_ATTEMPTS)

    run_started = now_iso()
    saved = 0
    skipped = 0
    failed = 0

    if not rows:
        summary = (
            f"DETAIL RUN started: {run_started}\n"
            f"DETAIL RUN finished: {now_iso()}\n"
            f"No pending detail rows.\n"
        )
        log_path = LOG_DIR / f"detail_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
        log_path.write_text(summary, encoding="utf-8")
        print(summary)
        return

    with sync_playwright() as p:
        browser = p.chromium.launch(
            executable_path=browser_executable(),
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--window-size=1280,720"
            ]
        )

        for row in rows:
            result = process_detail(browser, conn, row)
            if result == "saved":
                saved += 1
            elif result == "skipped_complete":
                skipped += 1
            else:
                failed += 1

        browser.close()

    pending = conn.execute("SELECT COUNT(*) AS c FROM opportunities WHERE detail_status != 'saved'").fetchone()["c"]

    summary = (
        f"DETAIL RUN started: {run_started}\n"
        f"DETAIL RUN finished: {now_iso()}\n"
        f"DETAIL_LIMIT: {DETAIL_LIMIT}\n"
        f"MAX_DETAIL_ATTEMPTS: {MAX_DETAIL_ATTEMPTS}\n"
        f"Rows selected: {len(rows)}\n"
        f"Saved: {saved}\n"
        f"Skipped complete: {skipped}\n"
        f"Failed: {failed}\n"
        f"Remaining pending: {pending}\n"
    )

    log_path = LOG_DIR / f"detail_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    log_path.write_text(summary, encoding="utf-8")
    print(summary)

if __name__ == "__main__":
    main()
