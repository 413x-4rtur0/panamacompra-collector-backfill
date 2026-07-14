#!/usr/bin/env python3
import os
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from common import *

def index_page_cap():
    """Return the optional per-status index page cap.

    0/auto/all means no normal cap: crawl until the portal disables Next. The
    separate hard safety cap prevents accidental infinite pagination loops.
    """
    raw = os.environ.get("PC_INDEX_LIMIT", os.environ.get("PC_MAX_PAGES_PER_GROUP", "0"))
    value = str(raw or "0").strip().lower()
    if value in {"", "0", "all", "auto", "none", "unlimited"}:
        return 0
    try:
        return max(1, int(value))
    except ValueError:
        return 0


MAX_PAGES_PER_GROUP = index_page_cap()
INDEX_HARD_SAFETY_CAP = env_int("PC_INDEX_HARD_SAFETY_CAP", "500", minimum=1)

ALL_GROUPS = [
    {"name": "Programadas", "radio_id": "btnradio2", "estado_prefix": "programad"},
    {"name": "Abiertas", "radio_id": "btnradio1", "estado_prefix": "abiert"},
]
# How many times to re-attempt a failed group switch (each attempt re-clicks the
# radio; the last ones reload the whole page first). Abiertas regularly needs a
# retry because the Angular table re-render races the radio click.
GROUP_SWITCH_ATTEMPTS = env_int("PC_INDEX_GROUP_SWITCH_ATTEMPTS", "3", minimum=1)
CRAWL_RESULT_ENV_PATH = QUEUE_DIR / "index_crawl_result.env"


def selected_groups():
    """Portal groups to crawl. PC_INDEX_GROUPS (comma-separated, e.g. "Abiertas")
    limits the crawl to specific groups — used by the worker when a changedetection
    snapshot already covered the others. Empty/unmatched values crawl everything,
    so a typo can never silently skip a group."""
    wanted = {g.strip().lower() for g in os.environ.get("PC_INDEX_GROUPS", "").split(",") if g.strip()}
    if not wanted:
        return ALL_GROUPS
    picked = [g for g in ALL_GROUPS if g["name"].lower() in wanted]
    return picked or ALL_GROUPS


GROUPS = selected_groups()


def index_start_pages(raw=None):
    """Per-group crawl start pages from "Group:page" pairs (comma-separated).

    Fed by PC_INDEX_START_PAGES (the worker copies SNAPSHOT_RECOVERY_PAGES from
    a partial changedetection snapshot), so the crawler resumes each unhealthy
    group at its first bad page instead of redoing the pages the snapshot
    already imported. Group names are case-insensitive; unknown groups and
    non-numeric pages are ignored so a malformed value can never break a crawl."""
    if raw is None:
        raw = os.environ.get("PC_INDEX_START_PAGES", "")
    canonical = {g["name"].lower(): g["name"] for g in ALL_GROUPS}
    starts = {}
    for part in str(raw or "").split(","):
        part = part.strip()
        if not part or ":" not in part:
            continue
        name, _, value = part.partition(":")
        group = canonical.get(name.strip().lower())
        if not group:
            continue
        try:
            page_number = int(value.strip())
        except ValueError:
            continue
        if page_number >= 1:
            starts[group] = page_number
    return starts


START_PAGES = index_start_pages()

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

def wait_for_table(page):
    page.wait_for_selector("tabla-busqueda-avanzada-v3 table", timeout=60000)
    page.wait_for_timeout(1500)

def page_signature(page):
    return page.evaluate("""
    (() => {
      const active = document.querySelector('ngb-pagination li.page-item.active a.page-link')?.innerText?.trim() || '';
      const first = document.querySelector('tabla-busqueda-avanzada-v3 tbody tr td a[href*="solicitud-de-cotizacion"], tabla-busqueda-avanzada-v3 tbody tr td a[href*="pliego-de-cargos"]')?.innerText?.trim() || '';
      const footer = document.querySelector('tabla-busqueda-avanzada-v3 .card')?.innerText?.trim() || '';
      const checked =
        document.querySelector('#btnradio2')?.checked ? 'Programadas' :
        document.querySelector('#btnradio1')?.checked ? 'Abiertas' :
        'Unknown';
      return checked + '|' + active + '|' + first + '|' + footer;
    })();
    """)

def prepare_base_page(page):
    page.goto(BASE_URL, wait_until="domcontentloaded", timeout=90000)
    page.wait_for_timeout(12000)
    close_popup(page)
    page.wait_for_timeout(2000)
    page.wait_for_selector("tabla-busqueda-avanzada-v3", timeout=60000)

def group_is_active(page, group_name, estado_prefix):
    """True when the requested radio is checked AND the table already shows at
    least one row whose ESTADO matches the group (e.g. 'Abierta' for Abiertas),
    so we never scrape the previous group's rows under the wrong label."""
    return page.evaluate("""
    ({groupName, estadoPrefix}) => {
      const norm = t => (t || '').toLowerCase().normalize('NFD').replace(/[\\u0300-\\u036f]/g, '').trim();
      const checked =
        document.querySelector('#btnradio2')?.checked ? 'Programadas' :
        document.querySelector('#btnradio1')?.checked ? 'Abiertas' : '';
      if (checked !== groupName) return false;
      const rows = Array.from(document.querySelectorAll('tabla-busqueda-avanzada-v3 tbody tr'));
      return rows.some(row => {
        const cells = Array.from(row.querySelectorAll('th, td')).map(td => norm(td.innerText));
        return cells.some(c => c.startsWith(estadoPrefix));
      });
    }
    """, {"groupName": group_name, "estadoPrefix": estado_prefix})

def click_status(page, group):
    """Switch the portal to a group's tab and confirm it really landed there.

    Returns True on success. Retries the radio click (the Angular re-render
    regularly races it, which is why Abiertas used to fail silently); the last
    attempt reloads the whole page first. On False the caller must SKIP the
    group instead of scraping whatever table is showing."""
    radio_id, group_name, estado_prefix = group["radio_id"], group["name"], group["estado_prefix"]
    for attempt in range(1, GROUP_SWITCH_ATTEMPTS + 1):
        if attempt == GROUP_SWITCH_ATTEMPTS and attempt > 1:
            print(f"{group_name}: reloading page for final switch attempt")
            prepare_base_page(page)
        close_popup(page)
        page.evaluate("""
        ({radioId}) => {
          const label = document.querySelector(`label[for="${radioId}"]`);
          const input = document.querySelector(`#${radioId}`);

          if (label) label.click();
          else if (input) {
            input.click();
            input.dispatchEvent(new Event('change', { bubbles: true }));
          }
        }
        """, {"radioId": radio_id})

        for _ in range(40):
            page.wait_for_timeout(500)
            if group_is_active(page, group_name, estado_prefix):
                page.wait_for_timeout(3500)
                close_popup(page)
                wait_for_table(page)
                return True
        print(f"{group_name}: switch attempt {attempt}/{GROUP_SWITCH_ATTEMPTS} failed (rows never showed '{estado_prefix}*')")
    return False

def set_rows_to_50(page):
    page.evaluate("""
    (() => {
      const select = document.querySelector('tabla-busqueda-avanzada-v3 select.form-select');
      if (!select) return false;

      const option50 = Array.from(select.options).find(o => o.textContent.trim() === '50');
      if (!option50) return false;

      if (select.value !== option50.value) {
        select.value = option50.value;
        select.selectedIndex = option50.index;
        select.dispatchEvent(new Event('input', { bubbles: true }));
        select.dispatchEvent(new Event('change', { bubbles: true }));
      }
      return true;
    })();
    """)
    page.wait_for_timeout(8000)
    wait_for_table(page)

def go_first_page(page):
    moved = page.evaluate("""
    (() => {
      const first = document.querySelector('ngb-pagination a[aria-label="First"]');
      if (!first) return false;
      const li = first.closest('li');
      if (li && li.classList.contains('disabled')) return false;
      first.click();
      return true;
    })();
    """)
    if moved:
        page.wait_for_timeout(5000)
        wait_for_table(page)

def extract_rows(page, group_name, page_number):
    return page.evaluate("""
    ({groupName, pageNumber}) => {
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

      function firstNumeroFrom(text) {
        const match = clean(text).match(/20\\d{2}-\\d+-\\d+-\\d+-\\d+-[A-Z]+-\\d+/);
        return match ? match[0] : '';
      }

      function findDetailLink(row) {
        const selector = 'a[href*="solicitud-de-cotizacion"], a[href*="pliego-de-cargos"]';
        const direct = row.querySelector(selector)?.getAttribute('href');
        if (direct) return absoluteUrl(direct);

        const anyHref = row.querySelector('a[href]')?.getAttribute('href');
        if (anyHref && (anyHref.includes('solicitud-de-cotizacion') || anyHref.includes('pliego-de-cargos'))) {
          return absoluteUrl(anyHref);
        }

        const html = row.innerHTML || '';
        const match = html.match(/(?:https?:\\/\\/[^'"\\s<>]+)?\\/Inicio\\/#\\/(?:solicitud-de-cotizacion|pliego-de-cargos)\\/[^'"\\s<>]+/);
        return match ? absoluteUrl(match[0]) : '';
      }

      const rows = Array.from(document.querySelectorAll('tabla-busqueda-avanzada-v3 tbody tr'));

      return rows.map(row => {
        const cells = Array.from(row.querySelectorAll('th, td')).map(td => clean(td.innerText));
        const link = findDetailLink(row);
        const numero = firstNumeroFrom(cells[1] || '') || firstNumeroFrom(row.innerText);

        return {
          grupo: groupName,
          source_page: pageNumber,
          visual_row: cells[0] || '',
          numero,
          estado: cells[2] || '',
          descripcion: cells[3] || '',
          entidad: cells[4] || '',
          dependencia: cells[5] || '',
          fecha: cells[6] || '',
          modalidad: cells[7] || '',
          link
        };
      }).filter(r => r.numero && r.link);
    }
    """, {"groupName": group_name, "pageNumber": page_number})

def click_next(page):
    available = page.evaluate("""
    (() => {
      const next = document.querySelector('ngb-pagination a[aria-label="Next"]');
      if (!next) return false;
      const li = next.closest('li');
      if (li && li.classList.contains('disabled')) return false;
      if (next.getAttribute('aria-disabled') === 'true') return false;
      return true;
    })();
    """)

    if not available:
        return False, "Next disabled or not found"

    before = page_signature(page)

    page.evaluate("""
    (() => {
      const next = document.querySelector('ngb-pagination a[aria-label="Next"]');
      if (next) next.click();
    })();
    """)

    for _ in range(40):
        page.wait_for_timeout(500)
        if page_signature(page) != before:
            page.wait_for_timeout(2500)
            return True, "Clicked next"

    return False, "Next clicked but page did not change"

def index_eta_text(pages_done, pages_budget, new_records, elapsed_seconds, prev_index_avg, prev_detail_avg):
    """Monitor ETA for the index phase plus expected detail time.

    When there is no configured page cap, the portal decides when indexing is
    done, so we only estimate known detail work instead of pretending there is a
    fixed index-page budget.
    """
    index_secs = eta_seconds_from_counts(pages_done, pages_budget, elapsed_seconds, prev_index_avg) if pages_budget else None
    detail_secs = new_records * prev_detail_avg if prev_detail_avg else 0
    if index_secs is None and not detail_secs:
        return "-"
    total = (index_secs or 0) + (detail_secs or 0)
    return f"{format_duration(total)} (índice + {new_records} det.)"


def main():
    conn = init_db()

    extracted_total = 0
    new_records = 0
    existing_records = 0
    json_written = 0
    json_skipped = 0
    duplicate_in_crawl = []
    seen = set()
    page_counts = []
    stop_reasons = []
    failed_groups = []
    run_started = now_iso()

    with sync_playwright() as p:
        browser = p.firefox.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--window-size=1280,720"
            ]
        )

        page = browser.new_page(viewport={"width": 1280, "height": 720})
        prepare_base_page(page)

        # Count-based ETA inputs: previous per-page and per-detail pace, plus a
        # crawl clock so the remaining-pages estimate refines live.
        prev_index_avg = previous_phase_avg_seconds(INDEX_TIMING_PATH)
        prev_detail_avg = previous_phase_avg_seconds(DETAIL_TIMING_PATH)
        index_start = time.monotonic()

        pages_done = 0
        total_pages_budget = len(GROUPS) * MAX_PAGES_PER_GROUP if MAX_PAGES_PER_GROUP else None

        for group in GROUPS:
            group_name = group["name"]

            if not click_status(page, group):
                stop_reasons.append(f"{group_name}: group switch FAILED after {GROUP_SWITCH_ATTEMPTS} attempts; group skipped")
                failed_groups.append(group_name)
                write_run_progress(
                    "INDEX", "RUNNING", 12,
                    f"Step 1/7: WARNING — could not open the {group_name} tab after {GROUP_SWITCH_ATTEMPTS} attempts; skipping it this run.",
                    step_current=1, step_total=7,
                    extra=f"group={group_name}; switch=failed",
                )
                continue
            set_rows_to_50(page)
            go_first_page(page)

            page_number = 1
            start_page = START_PAGES.get(group_name, 1)
            if start_page > 1:
                # Partial snapshot recovery: skip the pages the snapshot already
                # imported and resume this group at its first bad page.
                write_run_progress(
                    "INDEX", "RUNNING", 11,
                    f"Step 1/7: {group_name} resuming at page {start_page} (earlier pages imported from snapshot).",
                    step_current=1, step_total=7,
                    extra=f"group={group_name}; start_page={start_page}",
                )
                while page_number < start_page:
                    wait_for_table(page)
                    moved, why = click_next(page)
                    if not moved:
                        stop_reasons.append(f"{group_name}: could not advance to recovery page {start_page} ({why}); crawling from page {page_number}")
                        break
                    page_number += 1

            while True:
                if MAX_PAGES_PER_GROUP and page_number > MAX_PAGES_PER_GROUP:
                    stop_reasons.append(f"{group_name}: index page cap reached ({MAX_PAGES_PER_GROUP})")
                    break
                if not MAX_PAGES_PER_GROUP and page_number > INDEX_HARD_SAFETY_CAP:
                    stop_reasons.append(f"{group_name}: hard safety page cap reached ({INDEX_HARD_SAFETY_CAP})")
                    break

                wait_for_table(page)
                rows = extract_rows(page, group_name, page_number)
                # Safety net: keep only rows whose ESTADO belongs to this group
                # (blank estado tolerated in case the column layout shifts), so a
                # mid-crawl tab bounce can never store rows under the wrong group.
                rows = [
                    r for r in rows
                    if not r["estado"] or strip_accents(r["estado"]).lower().startswith(group["estado_prefix"])
                ]

                extracted_total += len(rows)
                page_counts.append((group_name, page_number, len(rows)))

                pages_done += 1
                overall_page = pages_done
                if total_pages_budget:
                    percent = 10 + int(40 * overall_page / total_pages_budget)
                else:
                    percent = min(49, 10 + (overall_page * 10))
                write_run_progress(
                    "INDEX",
                    "RUNNING",
                    percent,
                    f"Step 1/7: {group_name} page {page_number} collected {len(rows)} rows.",
                    step_current=1,
                    step_total=7,
                    item_current=overall_page,
                    item_total=total_pages_budget or "auto",
                    records_found=extracted_total,
                    records_new=new_records,
                    records_existing=existing_records,
                    eta=index_eta_text(overall_page - 1, total_pages_budget, new_records, time.monotonic() - index_start, prev_index_avg, prev_detail_avg),
                    extra=f"group={group_name}; page={page_number}; rows={len(rows)}",
                )

                for r in rows:
                    numero = r["numero"]

                    if numero in seen:
                        duplicate_in_crawl.append({
                            "numero": numero,
                            "grupo": group_name,
                            "page": page_number,
                            "visual_row": r.get("visual_row")
                        })
                        continue

                    seen.add(numero)

                    existing = find_existing_opportunity(conn, numero)

                    existing_on_disk = False
                    if existing:
                        date_folder = existing["date_folder"]
                        record_folder = Path(existing["record_folder"])
                        index_json_path = Path(existing["index_json_path"])
                    else:
                        disk_folder, disk_index_json = find_existing_record_archive(numero)
                        if disk_folder and disk_index_json:
                            existing_on_disk = True
                            date_folder = disk_folder.parent.name
                            record_folder = disk_folder
                            index_json_path = disk_index_json
                        else:
                            date_folder = date_folder_name()
                            record_folder = get_record_folder(date_folder, numero)
                            index_json_path = archive_index_json_path(record_folder, numero)

                    record_folder.mkdir(parents=True, exist_ok=True)

                    row = {
                        "numero": numero,
                        "grupo": group_name,
                        "tipo_url": detect_url_type(r["link"]),
                        "estado": r["estado"],
                        "descripcion": r["descripcion"],
                        "short_description": short_description(r["descripcion"]),
                        "entidad": r["entidad"],
                        "dependencia": r["dependencia"],
                        "fecha": r["fecha"],
                        "modalidad": r["modalidad"],
                        "link": r["link"],
                        "first_seen": existing["first_seen"] if existing else now_iso(),
                        "last_seen": now_iso(),
                        "date_folder": date_folder,
                        "record_folder": str(record_folder),
                        "index_json_path": str(index_json_path),
                        "detail_status": (
                            existing["detail_status"] if existing
                            else "saved" if existing_on_disk and archive_complete(record_folder, numero)
                            else "pending"
                        ),
                        "finish_date_guess": existing["finish_date_guess"] if existing else "",
                        "source_page_first_seen_or_last_seen": page_number,
                        "visual_row_first_seen_or_last_seen": r.get("visual_row")
                    }

                    result = insert_or_update_index(conn, row)

                    if result == "new" and not existing_on_disk:
                        new_records += 1
                    else:
                        existing_records += 1

                    # Immutable index JSON: write only once.
                    if write_json_once(index_json_path, row):
                        json_written += 1
                    else:
                        json_skipped += 1

                write_run_progress(
                    "INDEX",
                    "RUNNING",
                    percent,
                    f"Step 1/7: {group_name} page {page_number} processed. New={new_records}, existing={existing_records}.",
                    step_current=1,
                    step_total=7,
                    item_current=overall_page,
                    item_total=total_pages_budget or "auto",
                    records_found=extracted_total,
                    records_new=new_records,
                    records_existing=existing_records,
                    eta=index_eta_text(overall_page, total_pages_budget, new_records, time.monotonic() - index_start, prev_index_avg, prev_detail_avg),
                    extra=f"json_written={json_written}; json_skipped={json_skipped}; duplicates={len(duplicate_in_crawl)}",
                )

                moved, reason = click_next(page)
                if not moved:
                    stop_reasons.append(f"{group_name}: {reason}")
                    break
                page_number += 1

        browser.close()

    # Persist this crawl's per-page pace so the next run can show an ETA from
    # its very first page instead of waiting to measure its own speed.
    record_phase_timing(INDEX_TIMING_PATH, seconds=time.monotonic() - index_start, count=len(page_counts))

    # Publish per-group health so the run-all worker can alert on a skipped
    # group without failing the whole run (mirrors index_snapshot_result.env).
    ensure_dirs()
    crawl_fields = {
        "CRAWL_FAILED_GROUPS": ",".join(failed_groups),
        "CRAWL_GROUPS": ",".join(g["name"] for g in GROUPS),
        "CRAWL_WRITTEN_AT": now_iso(),
    }
    tmp = CRAWL_RESULT_ENV_PATH.with_suffix(CRAWL_RESULT_ENV_PATH.suffix + ".tmp")
    tmp.write_text("".join(f"{key}={shell_quote(value)}\n" for key, value in crawl_fields.items()), encoding="utf-8")
    tmp.replace(CRAWL_RESULT_ENV_PATH)

    db_total = conn.execute("SELECT COUNT(*) AS c FROM opportunities").fetchone()["c"]
    pending_details = conn.execute("SELECT COUNT(*) AS c FROM opportunities WHERE detail_status != 'saved'").fetchone()["c"]

    summary_lines = [
        f"INDEX RUN started: {run_started}",
        f"INDEX RUN finished: {now_iso()}",
        f"INDEX_PAGE_CAP: {MAX_PAGES_PER_GROUP or 'all'}",
        f"Stop reasons: {' | '.join(stop_reasons)}",
        "",
        f"Rows extracted total from site: {extracted_total}",
        f"Unique NUMERO values in this run: {len(seen)}",
        f"Duplicate NUMERO inside crawl: {len(duplicate_in_crawl)}",
        f"New DB records inserted: {new_records}",
        f"Existing DB records seen/updated: {existing_records}",
        f"Index JSON written: {json_written}",
        f"Index JSON skipped existing: {json_skipped}",
        "",
        f"DB total unique NUMERO records: {db_total}",
        f"DB detail pending/not saved: {pending_details}",
        "",
        "Page counts:",
    ]

    for group_name, page_num, qty in page_counts:
        summary_lines.append(f"  {group_name} page {page_num}: {qty} rows")

    if duplicate_in_crawl:
        summary_lines.append("")
        summary_lines.append("Duplicates inside crawl:")
        for d in duplicate_in_crawl[:100]:
            summary_lines.append(f"  {d['numero']} | {d['grupo']} page={d['page']} row={d['visual_row']}")

    summary = "\n".join(summary_lines) + "\n"

    write_run_progress(
        "INDEX",
        "DONE",
        50,
        f"Step 1/7 complete. Unique={len(seen)}, new={new_records}, existing={existing_records}, pending details={pending_details}.",
        step_current=1,
        step_total=7,
        item_current=len(page_counts),
        item_total=(len(GROUPS) * MAX_PAGES_PER_GROUP) if MAX_PAGES_PER_GROUP else len(page_counts),
        records_found=extracted_total,
        records_new=new_records,
        records_existing=existing_records,
        records_pending=pending_details,
        extra=f"db_total={db_total}; duplicates={len(duplicate_in_crawl)}",
    )

    log_path = LOG_DIR / f"index_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    log_path.write_text(summary, encoding="utf-8")
    print(summary)

if __name__ == "__main__":
    main()
