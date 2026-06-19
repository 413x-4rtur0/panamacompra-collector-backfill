#!/usr/bin/env python3
import json
import os
import re
from pathlib import Path
from playwright.sync_api import sync_playwright
from pc_common import *

DETAIL_LIMIT = env_int("PC_DETAIL_LIMIT", "10", minimum=0)
MAX_DETAIL_ATTEMPTS = env_int("PC_MAX_DETAIL_ATTEMPTS", "5", minimum=1)

# Bump when the link/table cleaning rules change so existing archives are
# refreshed from their saved HTML on the next run instead of keeping old noise.
# v3 also adds the summary / numbered items / calendar views to detail.json.
LINKS_SCHEMA_VERSION = 3

# Only keep genuinely useful links. The in-page extractors over-collect (every
# anchor, [onclick], and regex-matched URL in the HTML), which produced a lot of
# unwanted nav/router/asset links. We keep document attachments and the real
# PanamaCompra opportunity/portal links, and drop everything else.
_DESIRED_DOC_RE = re.compile(
    r"\.(pdf|docx?|xlsx?|pptx?|odt|ods|zip|rar|7z|csv|txt)(?:[?#]|$)", re.IGNORECASE
)

def is_desired_link(href):
    if not href:
        return False
    low = href.lower()
    if low.startswith(("javascript:", "mailto:", "tel:", "data:", "#")):
        return False
    if _DESIRED_DOC_RE.search(low):
        return True
    if "/solicitud-de-cotizacion/" in href or "/pliego-de-cargos/" in href:
        return True
    if "numlc=" in low or "vistapreviacp" in low or "escritorio" in low:
        return True
    return False

def filter_links(links):
    return [lk for lk in (links or []) if is_desired_link((lk or {}).get("href"))]

def clean_table(table):
    """Drop undesired links and add a key->value view for 2-column tables."""
    if "links" in table:
        table["links"] = filter_links(table.get("links"))
        table["links_count"] = len(table["links"])
    for key in ("rows_with_links", "raw_rows_with_links"):
        for row in table.get(key) or []:
            for cell in row:
                if isinstance(cell, dict) and "links" in cell:
                    cell["links"] = filter_links(cell.get("links"))
    kv = key_values_from_rows(table.get("raw_rows"))
    if kv:
        table["key_values"] = kv
    return table

def naming_fields(tables, text, row):
    """Compute (finish_stamp, desc_slug, proposed_folder_name) for a record."""
    agg_kv = {}
    for table in tables:
        agg_kv.update(table.get("key_values", {}))
    finish_stamp = compute_finish_stamp(agg_kv, text)
    desc_source = find_kv(agg_kv, "descripcion") or row["descripcion"] or row["short_description"]
    slug = desc_slug(desc_source)
    return finish_stamp, slug, build_record_folder_leaf(finish_stamp, row["numero"], slug)

# Rename the record folder to [finish]-[numero]-[desc] after a successful
# detail save. On by default; set PC_RENAME_AFTER_DETAIL=0 to keep <numero>.
RENAME_AFTER_DETAIL = os.environ.get("PC_RENAME_AFTER_DETAIL", "1") != "0"

def maybe_rename_folder(conn, row, proposed_folder_name):
    if not RENAME_AFTER_DETAIL or not proposed_folder_name:
        return
    # Nothing useful to encode (no finish date and no description): leave as-is.
    if proposed_folder_name == build_record_folder_leaf("", row["numero"], ""):
        return
    rename_record_folder(conn, row["numero"], row["record_folder"], proposed_folder_name)

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

def extract_links(page):
    return page.evaluate("""
    (() => {
      function clean(text) {
        return (text || '').replace(/\\s+/g, ' ').trim();
      }

      function absoluteUrl(href) {
        if (!href) return '';
        try {
          return new URL(href, window.location.href).href;
        } catch (e) {
          return href;
        }
      }

      function classify(href) {
        const lower = (href || '').toLowerCase();
        if (href.includes('/solicitud-de-cotizacion/')) return 'solicitud-de-cotizacion';
        if (href.includes('/pliego-de-cargos/')) return 'pliego-de-cargos';
        if (lower.match(/\\.(pdf|docx?|xlsx?|zip|rar|7z|csv|txt)(?:[?#]|$)/)) return 'document';
        return 'link';
      }

      function pushLink(links, seen, href, text, context) {
        href = absoluteUrl(href);
        if (!href || seen.has(href)) return;
        seen.add(href);
        links.push({
          link_index: links.length + 1,
          text: clean(text),
          href,
          kind: classify(href),
          context: clean(context),
        });
      }

      function collectLinks(root, context) {
        const links = [];
        const seen = new Set();

        root.querySelectorAll('a[href], [href], [data-href], [data-url]').forEach(el => {
          const href = el.getAttribute('href') || el.getAttribute('data-href') || el.getAttribute('data-url') || '';
          pushLink(links, seen, href, el.innerText || el.textContent || el.getAttribute('title') || '', context);
        });

        root.querySelectorAll('[onclick]').forEach(el => {
          const onclick = el.getAttribute('onclick') || '';
          const matches = onclick.match(/(?:https?:\\/\\/[^'"\\s<>]+|\\/Inicio\\/#\\/[^'"\\s<>]+|#\\/[^'"\\s<>]+)/g) || [];
          matches.forEach(href => pushLink(links, seen, href, el.innerText || el.textContent || '', context));
        });

        const html = root.innerHTML || '';
        const embedded = html.match(/(?:https?:\\/\\/[^'"\\s<>]+|\\/Inicio\\/#\\/[^'"\\s<>]+|#\\/[^'"\\s<>]+|[^'"\\s<>]+\\.(?:pdf|docx?|xlsx?|zip|rar|7z|csv|txt)(?:[?#][^'"\\s<>]*)?)/gi) || [];
        embedded.forEach(href => pushLink(links, seen, href, '', context));

        return links;
      }

      return collectLinks(document, 'page');
    })();
    """)

def extract_tables(page):
    return page.evaluate("""
    (() => {
      function clean(text) {
        return (text || '').replace(/\\s+/g, ' ').trim();
      }

      function absoluteUrl(href) {
        if (!href) return '';
        try {
          return new URL(href, window.location.href).href;
        } catch (e) {
          return href;
        }
      }

      function classify(href) {
        const lower = (href || '').toLowerCase();
        if (href.includes('/solicitud-de-cotizacion/')) return 'solicitud-de-cotizacion';
        if (href.includes('/pliego-de-cargos/')) return 'pliego-de-cargos';
        if (lower.match(/\\.(pdf|docx?|xlsx?|zip|rar|7z|csv|txt)(?:[?#]|$)/)) return 'document';
        return 'link';
      }

      function pushLink(links, seen, href, text) {
        href = absoluteUrl(href);
        if (!href || seen.has(href)) return;
        seen.add(href);
        links.push({ text: clean(text), href, kind: classify(href) });
      }

      function cellLinks(cell) {
        const links = [];
        const seen = new Set();
        cell.querySelectorAll('a[href], [href], [data-href], [data-url]').forEach(el => {
          const href = el.getAttribute('href') || el.getAttribute('data-href') || el.getAttribute('data-url') || '';
          pushLink(links, seen, href, el.innerText || el.textContent || el.getAttribute('title') || '');
        });
        cell.querySelectorAll('[onclick]').forEach(el => {
          const onclick = el.getAttribute('onclick') || '';
          const matches = onclick.match(/(?:https?:\\/\\/[^'"\\s<>]+|\\/Inicio\\/#\\/[^'"\\s<>]+|#\\/[^'"\\s<>]+)/g) || [];
          matches.forEach(href => pushLink(links, seen, href, el.innerText || el.textContent || ''));
        });
        const html = cell.innerHTML || '';
        const embedded = html.match(/(?:https?:\\/\\/[^'"\\s<>]+|\\/Inicio\\/#\\/[^'"\\s<>]+|#\\/[^'"\\s<>]+|[^'"\\s<>]+\\.(?:pdf|docx?|xlsx?|zip|rar|7z|csv|txt)(?:[?#][^'"\\s<>]*)?)/gi) || [];
        embedded.forEach(href => pushLink(links, seen, href, ''));
        return links;
      }

      return Array.from(document.querySelectorAll('table')).map((table, idx) => {
        const structuredRows = Array.from(table.querySelectorAll('tr')).map(tr =>
          Array.from(tr.querySelectorAll('th, td')).map(td => ({
            text: clean(td.innerText),
            links: cellLinks(td),
          }))
        ).filter(r => r.some(cell => cell.text || cell.links.length));

        const rows = structuredRows.map(row => row.map(cell => cell.text));
        let headers = [];
        let dataRows = rows;
        let dataRowsWithLinks = structuredRows;

        if (rows.length > 0) {
          headers = rows[0];
          dataRows = rows.slice(1);
          dataRowsWithLinks = structuredRows.slice(1);
        }

        const links = [];
        const seen = new Set();
        structuredRows.forEach((row, rowIndex) => {
          row.forEach((cell, cellIndex) => {
            cell.links.forEach(link => {
              if (seen.has(link.href)) return;
              seen.add(link.href);
              links.push({ ...link, row_index: rowIndex + 1, cell_index: cellIndex + 1 });
            });
          });
        });

        return {
          table_index: idx + 1,
          headers,
          rows: dataRows,
          raw_rows: rows,
          rows_with_links: dataRowsWithLinks,
          raw_rows_with_links: structuredRows,
          links_count: links.length,
          links,
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

def save_table_jsons(record_folder, numero, tables, overwrite=False):
    tables_dir = record_folder / "tables"
    tables_dir.mkdir(parents=True, exist_ok=True)

    written = 0
    for table in tables:
        path = tables_dir / f"{safe_name(numero)}.table_{table['table_index']:03d}.json"
        if overwrite:
            path.write_text(json.dumps(table, ensure_ascii=False, indent=2), encoding="utf-8")
            written += 1
        elif write_json_once(path, table):
            written += 1

    return written

def detail_archive_has_link_metadata(detail_json_path):
    try:
        data = json.loads(detail_json_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return False

    if "links_detected" not in data or "links_count" not in data:
        return False

    # Re-process when the cleaning rules changed (also adds key_values and
    # strips old unwanted links from already-saved archives).
    if data.get("links_schema_version") != LINKS_SCHEMA_VERSION:
        return False

    # The structured views are part of the current schema.
    if "summary" not in data or "calendar" not in data:
        return False

    table_paths = list((detail_json_path.parent / "tables").glob("*.json"))
    if not table_paths:
        return True

    for table_path in table_paths:
        try:
            table = json.loads(table_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return False
        if "rows_with_links" not in table or "links" not in table:
            return False

    return True

def refresh_link_metadata_from_saved_html(browser, row, html_path, detail_json_path):
    page = browser.new_page(viewport={"width": 1280, "height": 720})
    try:
        page.set_content(html_path.read_text(encoding="utf-8", errors="ignore"), wait_until="domcontentloaded")
        tables = [clean_table(t) for t in extract_tables(page)]
        links = filter_links(extract_links(page))
    finally:
        page.close()

    save_table_jsons(Path(row["record_folder"]), row["numero"], tables, overwrite=True)

    n = safe_name(row["numero"])
    txt_path = Path(row["record_folder"]) / f"{n}.detail.txt"
    text = txt_path.read_text(encoding="utf-8", errors="ignore") if txt_path.exists() else ""
    finish_stamp, slug, proposed_folder_name = naming_fields(tables, text, row)

    try:
        detail_data = json.loads(detail_json_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        detail_data = {
            "numero": row["numero"],
            "grupo": row["grupo"],
            "tipo_url": row["tipo_url"],
            "link": row["link"],
            "source": "PanamaCompra",
            "saved_at": now_iso(),
        }

    summary, items, calendar, fields_detected = build_detail_views(
        text, tables, row["numero"], dtstamp=detail_data.get("saved_at")
    )

    detail_data.update({
        "links_count": len(links),
        "links_detected": links,
        "links_schema_version": LINKS_SCHEMA_VERSION,
        "tables_count": len(tables),
        "tables_refreshed_for_links_at": now_iso(),
        "finish_stamp": finish_stamp,
        "desc_slug": slug,
        "proposed_folder_name": proposed_folder_name,
        "summary": summary,
        "items_count": len(items),
        "items": items,
        "calendar": calendar,
        "fields_detected": fields_detected,
        "views_schema_version": VIEWS_SCHEMA_VERSION,
    })
    detail_json_path.write_text(json.dumps(detail_data, ensure_ascii=False, indent=2), encoding="utf-8")
    write_calendar_ics(Path(row["record_folder"]) / f"{n}.calendar.ics", calendar)
    return proposed_folder_name

def process_detail(browser, conn, row, force=False):
    """Fetch and archive one record's detail page.

    With ``force=True`` the live page is re-fetched and the saved HTML / text /
    detail JSON / calendar ICS / table JSONs are overwritten in place (used by the manual
    day-folder updater to re-pull records as the current portal version);
    otherwise an already-complete record is skipped or just re-cleaned.
    """
    numero = row["numero"]
    record_folder = Path(row["record_folder"])
    record_folder.mkdir(parents=True, exist_ok=True)

    n = safe_name(numero)
    html_path = record_folder / f"{n}.detail.html"
    txt_path = record_folder / f"{n}.detail.txt"
    detail_json_path = record_folder / f"{n}.detail.json"

    if not force and html_path.exists() and txt_path.exists() and detail_json_path.exists():
        if not detail_archive_has_link_metadata(detail_json_path):
            proposed = refresh_link_metadata_from_saved_html(browser, row, html_path, detail_json_path)
            update_detail_status(conn, numero, "saved", detail_json_path=detail_json_path)
            maybe_rename_folder(conn, row, proposed)
            return "refreshed_links"

        update_detail_status(conn, numero, "saved", detail_json_path=detail_json_path)
        return "skipped_complete"

    page = browser.new_page(viewport={"width": 1280, "height": 720})

    try:
        page.goto(row["link"], wait_until="domcontentloaded", timeout=90000)
        page.wait_for_timeout(12000)
        close_popup(page)

        html = page.content()
        text = page.locator("body").inner_text(timeout=25000)
        tables = [clean_table(t) for t in extract_tables(page)]
        links = filter_links(extract_links(page))
        label_values = extract_label_values_from_text(text)
        finish_date_guess = guess_finish_date_from_text(text)
        finish_stamp, slug, proposed_folder_name = naming_fields(tables, text, row)
        saved_at = now_iso()
        summary, items, calendar, fields_detected = build_detail_views(
            text, tables, numero, dtstamp=saved_at
        )

        if force:
            html_path.write_text(html, encoding="utf-8", errors="ignore")
            txt_path.write_text(text, encoding="utf-8", errors="ignore")
        else:
            write_text_once(html_path, html)
            write_text_once(txt_path, text)
        tables_written = save_table_jsons(record_folder, numero, tables, overwrite=force)

        detail_data = {
            "numero": numero,
            "grupo": row["grupo"],
            "tipo_url": row["tipo_url"],
            "link": row["link"],
            "source": "PanamaCompra",
            "saved_at": saved_at,
            "finish_date_guess": finish_date_guess,
            "short_description": row["short_description"],
            "descripcion_index": row["descripcion"],
            "entidad_index": row["entidad"],
            "dependencia_index": row["dependencia"],
            "fecha_index": row["fecha"],
            "modalidad_index": row["modalidad"],
            "label_values_detected": label_values,
            "summary": summary,
            "items_count": len(items),
            "items": items,
            "calendar": calendar,
            "fields_detected": fields_detected,
            "views_schema_version": VIEWS_SCHEMA_VERSION,
            "tables_count": len(tables),
            "tables_written_now": tables_written,
            "links_count": len(links),
            "links_detected": links,
            "links_schema_version": LINKS_SCHEMA_VERSION,
            "finish_stamp": finish_stamp,
            "desc_slug": slug,
            "proposed_folder_name": proposed_folder_name,
            "files": {
                "index_json": row["index_json_path"],
                "detail_json": str(detail_json_path),
                "detail_html": str(html_path),
                "detail_txt": str(txt_path),
                "calendar_ics": str(record_folder / f"{n}.calendar.ics"),
                "tables_folder": str(record_folder / "tables"),
            }
        }

        if force:
            detail_json_path.write_text(json.dumps(detail_data, ensure_ascii=False, indent=2), encoding="utf-8")
        else:
            write_json_once(detail_json_path, detail_data)
        if force or not (record_folder / f"{n}.calendar.ics").exists():
            write_calendar_ics(record_folder / f"{n}.calendar.ics", calendar)
        update_detail_status(conn, numero, "saved", detail_json_path=detail_json_path, finish_date_guess=finish_date_guess)
        maybe_rename_folder(conn, row, proposed_folder_name)

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
        write_run_progress(
            "DETAIL",
            "DONE",
            100,
            "Step 2/2 complete. No pending detail rows.",
            step_current=2,
            step_total=2,
            item_current=0,
            item_total=0,
            records_pending=0,
        )

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

        total_rows = len(rows)
        for index, row in enumerate(rows, start=1):
            percent = 55 + int(40 * (index - 1) / max(total_rows, 1))
            write_run_progress(
                "DETAIL",
                "RUNNING",
                percent,
                f"Step 2/2: downloading detail {index}/{total_rows}: {row['numero']}",
                step_current=2,
                step_total=2,
                item_current=index,
                item_total=total_rows,
                records_saved=saved + skipped,
                records_failed=failed,
                extra=f"current_numero={row['numero']}",
            )

            result = process_detail(browser, conn, row)
            if result == "saved":
                saved += 1
            elif result in ("skipped_complete", "refreshed_links"):
                skipped += 1
            else:
                failed += 1

            write_run_progress(
                "DETAIL",
                "RUNNING",
                55 + int(40 * index / max(total_rows, 1)),
                f"Step 2/2: processed detail {index}/{total_rows}. Saved/skipped={saved + skipped}, failed={failed}.",
                step_current=2,
                step_total=2,
                item_current=index,
                item_total=total_rows,
                records_saved=saved + skipped,
                records_failed=failed,
                extra=f"last_numero={row['numero']}; result={result}",
            )

        browser.close()

    pending = conn.execute("SELECT COUNT(*) AS c FROM opportunities WHERE detail_status != 'saved'").fetchone()["c"]

    write_run_progress(
        "DETAIL",
        "DONE",
        98,
        f"Step 2/2 complete. Saved/skipped={saved + skipped}, failed={failed}, remaining pending={pending}.",
        step_current=2,
        step_total=2,
        item_current=len(rows),
        item_total=len(rows),
        records_saved=saved + skipped,
        records_failed=failed,
        records_pending=pending,
    )

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
