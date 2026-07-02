#!/usr/bin/env python3
import json
import os
import re
import sys
import time
from pathlib import Path
from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from common import *

# Optional WAHA baseline helper. This does not send messages; it only marks the
# pre-existing archive before new detail rows are saved so later messaging can
# distinguish genuinely new records from the historical baseline.
try:
    pc_notify = load_script("src/pipeline/020-notify-whatsapp.py", "notify_new_records")
except Exception:  # noqa: BLE001 - notifications are strictly optional
    pc_notify = None

DETAIL_LIMIT = env_int("PC_DETAIL_LIMIT", "10", minimum=0)
MAX_DETAIL_ATTEMPTS = env_int("PC_MAX_DETAIL_ATTEMPTS", "5", minimum=1)
# Content-sanity floor for a rendered detail page. A real PanamaCompra detail
# page produces long body text AND structured data (tables / detected fields).
# When the body is shorter than this AND nothing structured was extracted, the
# portal almost certainly returned an error or blank shell with a 200 status (or
# the render was cut short), so the page is failed and retried instead of being
# saved as a hollow "complete" record. Set PC_DETAIL_MIN_TEXT_CHARS=0 to disable.
DETAIL_MIN_TEXT_CHARS = env_int("PC_DETAIL_MIN_TEXT_CHARS", "400", minimum=0)

# Bump when the link/table cleaning rules change so existing archives are
# refreshed from their saved HTML on the next run instead of keeping old noise.
# v3 also adds the summary / numbered items / calendar views to detail.json.
LINKS_SCHEMA_VERSION = 4

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

      function sectionTitleFor(table) {
        const sel = 'h1,h2,h3,h4,h5,h6,legend,.panel-title,.card-title,.card-header,.section-title,.titulo,.title';
        function fromEl(el) {
          if (!el || !el.matches) return '';
          if (el.matches(sel)) return clean(el.innerText || el.textContent || '');
          const h = el.querySelector ? el.querySelector(sel) : null;
          if (h) return clean(h.innerText || h.textContent || '');
          const t = clean(el.innerText || el.textContent || '');
          if (t && t.length <= 60 && /:\\s*$/.test(t)) return t.replace(/:\\s*$/, '');
          return '';
        }
        for (let node = table; node; node = node.parentElement) {
          let sib = node.previousElementSibling;
          while (sib) {
            const t = fromEl(sib);
            if (t) return t;
            sib = sib.previousElementSibling;
          }
        }
        return '';
      }

      return Array.from(document.querySelectorAll('table')).map((table, idx) => {
        const section = sectionTitleFor(table);
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
          section,
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

def row_archive_is_current(row):
    # A saved record counts as "current" once its archive files exist. Refreshing
    # already-saved ("previous") records to newer parsing/schema is intentionally
    # manual-only (040-build-detail-views.py / 080-update-day-folder.py), so a normal
    # run never re-pulls previous records just because a schema version changed —
    # it only completes saved rows whose files are actually missing on disk.
    return archive_complete(Path(row["record_folder"]), row["numero"])


def detail_pending_rows(conn, limit, max_attempts):
    # limit <= 0 means no batch cap: process every pending/currentness-missing
    # detail row. Manual/test controls pass positive limits when a bounded run is
    # desired; automatic changedetection runs use 0 so one index row can flow to
    # its detail without an artificial cap.

    # Skip rows that have already failed too many times, so a permanently broken
    # URL is not retried forever and cannot starve newer rows. Rows with fewer
    # attempts are processed first. Also include rows marked saved in SQLite but
    # missing the current archive files/views on disk, which can happen after
    # migrating older records or when an earlier run only wrote the index JSON.
    candidates = conn.execute("""
    SELECT *
    FROM opportunities
    WHERE detail_attempts < ?
    ORDER BY
      CASE WHEN detail_status = 'saved' THEN 1 ELSE 0 END,
      detail_attempts ASC,
      first_seen ASC
    """, (max_attempts,)).fetchall()
    pending = []
    for row in candidates:
        if row["detail_status"] != "saved" or not row_archive_is_current(row):
            pending.append(row)
            if limit > 0 and len(pending) >= limit:
                break
    return pending

# save_table_jsons now lives in pc_common (browser-free, shared with
# 040-build-detail-views.py) and is imported via `from common import *`.

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

    # The structured views and the per-section tables index are part of the
    # current schema. Individual table files are split (clean/raw/raw_wL), so the
    # detail.json markers are authoritative rather than inspecting each file.
    if "summary" not in data or "calendar" not in data or "tables" not in data:
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

    _, table_descriptors = save_table_jsons(Path(row["record_folder"]), row["numero"], tables, overwrite=True)

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
        text, tables, row["numero"], dtstamp=detail_data.get("saved_at"),
        link=detail_data.get("link") or row["link"],
    )

    detail_data.update({
        "links_count": len(links),
        "links_detected": links,
        "links_schema_version": LINKS_SCHEMA_VERSION,
        "tables_count": len(tables),
        "tables": table_descriptors,
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
        # "Complete" here must mean the SAME thing as archive_complete (the gate
        # detail_pending_rows re-queues on): current link schema AND all required
        # files present on disk, including the .ics and the summary/items/calendar
        # views. Otherwise a record missing only its .ics (e.g. interrupted right
        # after detail.json was written) is skipped as "complete" forever while
        # the queue keeps re-selecting it. When anything is missing we rebuild the
        # views/.ics from the saved HTML instead of re-marking it saved blindly.
        if not (detail_archive_has_link_metadata(detail_json_path)
                and archive_complete(record_folder, numero)):
            proposed = refresh_link_metadata_from_saved_html(browser, row, html_path, detail_json_path)
            update_detail_status(conn, numero, "saved", detail_json_path=detail_json_path,
                                 increment_attempts=False)
            maybe_rename_folder(conn, row, proposed)
            return "refreshed_links"

        update_detail_status(conn, numero, "saved", detail_json_path=detail_json_path,
                             increment_attempts=False)
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
            text, tables, numero, dtstamp=saved_at, link=row["link"]
        )

        # Content-sanity gate. Only fail when every signal says the page is empty
        # (short body AND no tables AND no label values AND no parsed fields), so a
        # genuine record — which always yields tables and/or fields — is never
        # rejected. This catches the "200 with an error/blank shell" case that
        # would otherwise be persisted as a hollow, never-revisited record; the
        # raise routes into the existing failure path (error file + status=failed),
        # so the record is retried next run and by the STEP 4 repair.
        body_chars = len((text or "").strip())
        if (DETAIL_MIN_TEXT_CHARS and body_chars < DETAIL_MIN_TEXT_CHARS
                and not tables and not label_values and not fields_detected):
            raise ValueError(
                f"detail page looks empty for {numero}: {body_chars} body chars, "
                f"0 tables, no fields detected (min {DETAIL_MIN_TEXT_CHARS}); "
                "not saving, will retry"
            )

        start_date_guess = calendar.get("dtstart") or ""
        # Keep the DB deadline coherent with the folder finish-stamp and the
        # per-record .ics: all three derive from the same calendar close window.
        # The looser free-text guess is only a last resort when that found nothing.
        finish_date_guess = calendar.get("dtend") or finish_stamp or finish_date_guess
        summary["date_start_opportunity"] = start_date_guess
        summary["date_end_opportunity"] = calendar.get("dtend") or finish_date_guess
        summary["date_downloaded_local"] = saved_at
        summary["date_name_finish_stamp"] = finish_stamp

        if force:
            html_path.write_text(html, encoding="utf-8", errors="ignore")
            txt_path.write_text(text, encoding="utf-8", errors="ignore")
        else:
            write_text_once(html_path, html)
            write_text_once(txt_path, text)
        tables_written, table_descriptors = save_table_jsons(record_folder, numero, tables, overwrite=force)
        detail_sections_written, detail_section_descriptors = save_detail_section_jsons(
            record_folder,
            numero,
            {
                "summary": summary,
                "items": items,
                "calendar": calendar,
                "fields_detected": fields_detected,
                "links_detected": links,
            },
            overwrite=force,
        )

        detail_data = {
            "numero": numero,
            "grupo": row["grupo"],
            "tipo_url": row["tipo_url"],
            "link": row["link"],
            "source": "PanamaCompra",
            "saved_at": saved_at,
            "finish_date_guess": finish_date_guess,
            "start_date_guess": start_date_guess,
            "date_start_opportunity": start_date_guess,
            "date_end_opportunity": calendar.get("dtend") or finish_date_guess,
            "date_downloaded_local": saved_at,
            "date_name_finish_stamp": finish_stamp,
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
            "detail_sections_count": len(detail_section_descriptors),
            "detail_sections_written_now": detail_sections_written,
            "detail_sections": detail_section_descriptors,
            "tables_count": len(tables),
            "tables_written_now": tables_written,
            "tables": table_descriptors,
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
        update_detail_status(conn, numero, "saved", detail_json_path=detail_json_path, finish_date_guess=finish_date_guess, start_date_guess=start_date_guess)
        conn.execute(
            """
            UPDATE opportunities
            SET record_folder_leaf = ?,
                files_layout_version = ?,
                detail_sections_count = ?,
                tables_count = ?,
                db_reviewed_at = ?
            WHERE numero = ?
            """,
            (
                record_folder.name,
                2,
                len([key for key in detail_section_descriptors if key != "_all"]),
                len(table_descriptors),
                now_iso(),
                numero,
            ),
        )
        conn.commit()
        maybe_rename_folder(conn, row, proposed_folder_name)

        return "saved"

    except Exception as e:
        err_path = record_folder / f"{n}.error.txt"
        write_text_once(err_path, str(e))
        update_detail_status(conn, numero, "failed")
        return "failed"

    finally:
        page.close()

def detail_eta_text(done, total, elapsed_seconds, prev_avg_seconds):
    """Monitor ETA string for the detail phase: estimated time for the detail
    pages still pending in this batch. '-' when no pace is known yet."""
    secs = eta_seconds_from_counts(done, total, elapsed_seconds, prev_avg_seconds)
    if secs is None:
        return "-"
    remaining = max(0, int(total) - int(done))
    return f"{format_duration(secs)} (~{remaining} pendiente(s))"


def main():
    conn = init_db()

    if pc_notify is not None:
        try:
            pc_notify.ensure_baseline(conn)
        except Exception as exc:  # noqa: BLE001 - baseline setup never blocks a run
            print(f"WAHA baseline check failed: {exc}", file=sys.stderr)

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
            "Step 3/7 complete. No pending detail rows.",
            step_current=3,
            step_total=7,
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
        browser = p.firefox.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--window-size=1280,720"
            ]
        )

        total_rows = len(rows)
        # Count-based ETA: time each detail page and project the ones still
        # pending. The first item has no live measurement yet, so it falls back
        # to the average seconds/detail recorded by the previous run.
        prev_detail_avg = previous_phase_avg_seconds(DETAIL_TIMING_PATH)
        loop_start = time.monotonic()
        for index, row in enumerate(rows, start=1):
            percent = 55 + int(40 * (index - 1) / max(total_rows, 1))
            elapsed = time.monotonic() - loop_start
            write_run_progress(
                "DETAIL",
                "RUNNING",
                percent,
                f"Step 3/7: downloading detail {index}/{total_rows}: {row['numero']}",
                step_current=3,
                step_total=7,
                item_current=index,
                item_total=total_rows,
                records_saved=saved + skipped,
                records_failed=failed,
                eta=detail_eta_text(index - 1, total_rows, elapsed, prev_detail_avg),
                extra=f"current_numero={row['numero']}",
            )

            result = process_detail(browser, conn, row)
            if result == "saved":
                saved += 1
            elif result in ("skipped_complete", "refreshed_links"):
                skipped += 1
            else:
                failed += 1

            elapsed = time.monotonic() - loop_start
            write_run_progress(
                "DETAIL",
                "RUNNING",
                55 + int(40 * index / max(total_rows, 1)),
                f"Step 3/7: processed detail {index}/{total_rows}. Saved/skipped={saved + skipped}, failed={failed}.",
                step_current=3,
                step_total=7,
                item_current=index,
                item_total=total_rows,
                records_saved=saved + skipped,
                records_failed=failed,
                eta=detail_eta_text(index, total_rows, elapsed, prev_detail_avg),
                extra=f"last_numero={row['numero']}; result={result}",
            )

        # Remember this run's pace so the next run can show an ETA immediately.
        record_phase_timing(DETAIL_TIMING_PATH, seconds=time.monotonic() - loop_start, count=total_rows)
        browser.close()

    pending = conn.execute("SELECT COUNT(*) AS c FROM opportunities WHERE detail_status != 'saved'").fetchone()["c"]

    write_run_progress(
        "DETAIL",
        "DONE",
        98,
        f"Step 3/7 complete. Saved/skipped={saved + skipped}, failed={failed}, remaining pending={pending}.",
        step_current=3,
        step_total=7,
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
