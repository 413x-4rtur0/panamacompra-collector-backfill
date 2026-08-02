#!/usr/bin/env python3
"""Low-resource background index crawl for the PanamaCompra 'Cerradas'
(closed) tab — deliberately separate from 010-collect-index.py (which only
ever crawls Abiertas/Programadas) so the main every-~30-minute pipeline never
has to pay for the closed archive's much larger page count.

Two modes (PC_CLOSED_MODE):
  forward  (default) — page 1 onward, catches newly-closed opportunities.
           Same stop condition as backfill (paginate until every row on a
           page is older than the cutoff date), not a fixed page count — see
           forward_target_days()/PC_CLOSED_FORWARD_DAYS (default 7).
           PC_CLOSED_FORWARD_PAGES is only a runaway-safety ceiling now,
           raised well above what a normal run should ever need.
  backfill — resumes from the single-row closed_crawl_state cursor,
           working backward through the historical archive a bounded chunk of
           pages at a time (PC_CLOSED_BACKFILL_PAGES), stopping and marking
           itself complete as soon as it reaches PC_CLOSED_BACKFILL_DAYS
           (default 365) of history — not a fixed page count, since page
           density varies. Staff can reset the cursor from the monitor, or
           raise PC_CLOSED_BACKFILL_DAYS to go deeper.

           Alternatively, PC_CLOSED_BACKFILL_START_DATE/END_DATE (YYYY-MM-DD)
           target a specific date range instead of the day count: START_DATE
           replaces the cutoff outright (crawl stops once it reaches that
           date), END_DATE makes the crawl skip-without-inserting any row
           newer than it until paging naturally reaches the range. Reset the
           cursor from the monitor before starting a new range.

PC_CLOSED_GROUP selects which portal tab this run crawls: "Closed" (default,
btnradio3/"Cerradas") or "Cancelled" (btnradio4/"Canceladas") — see
ALL_GROUPS/selected_group(). closed_crawl_state keeps one cursor row per
group (see get_closed_crawl_state/update_closed_crawl_state), so both modes
support either group independently — a Cancelled backfill run never touches
or corrupts the Closed cursor and vice versa.
Priority 2 (070-run-collector-closed-new.sh) runs this script once per group.

Only writes to `opportunities` (grupo=<selected group>, detail_status=
'pending' — kept out of 030-collect-details.py's own-pipeline queue by that
script's grupo check, see its detail_pending_rows(); picked up instead by
037b-collect-closed-details.py on this feature's own low-resource schedule).
The separate 038-collect-cotizaciones.py picks up the actual price/provider
data from there — though a Cancelled record never has any: it never reaches
the cuadro-de-cotizaciones stage, so it always resolves to 'no_link'.
"""
import os
import re
import sys
import time
from datetime import timedelta
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from common import *

# estado_prefix stays the site's own Spanish ESTADO text on purpose
# ("Cerrada"/"Cerradas", "Cancelada"/"Cancelado"), which never changes no
# matter what this codebase calls the concept internally.
ALL_GROUPS = [
    {"name": "Closed", "radio_id": "btnradio3", "estado_prefix": "cerrad"},
    {"name": "Cancelled", "radio_id": "btnradio4", "estado_prefix": "cancelad"},
]


def selected_group():
    """Which portal tab this run crawls, from PC_CLOSED_GROUP (default
    "Closed" — every existing caller/env is unaffected by this option
    existing). Priority 2 (070-run-collector-closed-new.sh) runs this script
    once per group so a problem in one group's crawl can never affect the
    other's. Backfill mode also respects this setting, using the matching
    per-group row in closed_crawl_state (see get_closed_crawl_state)."""
    wanted = str(os.environ.get("PC_CLOSED_GROUP", "") or "").strip().lower()
    if not wanted:
        return ALL_GROUPS[0]
    for group in ALL_GROUPS:
        if group["name"].lower() == wanted:
            return group
    return ALL_GROUPS[0]


GROUP = selected_group()
GROUP_SWITCH_ATTEMPTS = env_int("PC_INDEX_GROUP_SWITCH_ATTEMPTS", "3", minimum=1)


def closed_mode() -> str:
    value = str(os.environ.get("PC_CLOSED_MODE", "forward") or "forward").strip().lower()
    return value if value in ("forward", "backfill") else "forward"


def forward_page_cap() -> int:
    # Safety ceiling only — forward_target_days()/reached_cutoff_date is the
    # real stop condition now (see main()), same relationship backfill has
    # between backfill_page_cap() and backfill_cutoff_date().
    return env_int("PC_CLOSED_FORWARD_PAGES", "20", minimum=1)


def forward_target_days() -> int:
    return env_int("PC_CLOSED_FORWARD_DAYS", "7", minimum=1)


def forward_cutoff_date():
    return (datetime.now() - timedelta(days=forward_target_days())).date()


def backfill_page_cap() -> int:
    # Raised from the original 5: the backfill loop now stops itself as soon
    # as it reaches backfill_target_days() of history (see
    # backfill_cutoff_date()), so a higher per-run ceiling just lets a
    # scheduled run make real progress toward that date instead of needing
    # dozens of runs to get there a handful of pages at a time.
    return env_int("PC_CLOSED_BACKFILL_PAGES", "40", minimum=1)


def backfill_target_days() -> int:
    return env_int("PC_CLOSED_BACKFILL_DAYS", "365", minimum=1)


def backfill_cutoff_date():
    return (datetime.now() - timedelta(days=backfill_target_days())).date()


def backfill_range_bound(env_name: str):
    """A staff-supplied YYYY-MM-DD bound (start or end) for a targeted date-
    range backfill, or None when unset/unparseable — falls back to the
    day-count cutoff (backfill_cutoff_date) rather than erroring."""
    raw = str(os.environ.get(env_name, "") or "").strip()
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError:
        return None


def backfill_start_date():
    return backfill_range_bound("PC_CLOSED_BACKFILL_START_DATE")


def backfill_end_date():
    return backfill_range_bound("PC_CLOSED_BACKFILL_END_DATE")


def parse_dmy_date(text):
    """Parse the Closed listing's own FECHA cell ('DD/MM/YYYY', optionally
    with a trailing time) into a date, or None if it doesn't match — same
    date format every other PanamaCompra field in this codebase uses."""
    m = re.match(r"(\d{1,2})[-/](\d{1,2})[-/](\d{4})", (text or "").strip())
    if not m:
        return None
    day, month, year = (int(g) for g in m.groups())
    try:
        return datetime(year, month, day).date()
    except ValueError:
        return None


def apply_native_date_filter(page, start_date, end_date) -> bool:
    """Drive the site's own Fecha Desde/Fecha Hasta filter on the advanced
    search table, instead of paging the full unfiltered listing and
    filtering client-side. The site has a real bug: clicking a status radio
    (Cerradas) first silently discards a pending date filter, but clicking
    Buscar first (while still on whichever tab is active) commits it, and
    the status click afterward correctly re-applies the committed filter.
    Confirmed by hand against the live site — this exact order is required,
    reordering breaks it.

    Pressing Escape after typing into either date field is NOT equivalent to
    just dismissing the calendar popup: it discards the just-typed value
    back to whatever the field held before (confirmed live — a run that
    presses Escape here silently searches the field's old/default value
    instead of the requested date, with no error). Click somewhere neutral
    on the page to blur the field instead, which closes the popup without
    reverting the value.

    Returns True if the filter inputs were found and submitted (not a
    guarantee the server honored it, just that we tried).
    """
    try:
        for input_id, value in (("fd", start_date), ("fh", end_date)):
            field = page.locator(f"#{input_id}")
            field.click()
            page.keyboard.press("Control+A")
            page.keyboard.type(value.strftime("%d-%m-%Y"), delay=25)
        # Blur both fields by clicking a neutral element -- NOT Escape (see
        # docstring). Any non-interactive element works; the page heading is
        # always present regardless of which tab/group is active.
        page.locator("h1, h2").first.click()
        page.wait_for_timeout(500)
        # Two "Buscar" buttons exist (Numero search, date/entidad search) —
        # the second one is the date filter's. Must click while still on the
        # default Abiertas tab; click_group() switches to the target tab after.
        page.locator("button", has_text="Buscar").nth(1).click()
        page.wait_for_timeout(2500)
        return True
    except Exception as exc:
        print(f"Closed: native date filter failed ({exc}); falling back to full pagination")
        return False


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


def page_signature(page, group):
    return page.evaluate("""
    ({radioId, groupName}) => {
      const active = document.querySelector('ngb-pagination li.page-item.active a.page-link')?.innerText?.trim() || '';
      const first = document.querySelector('tabla-busqueda-avanzada-v3 tbody tr td a[href*="solicitud-de-cotizacion"], tabla-busqueda-avanzada-v3 tbody tr td a[href*="pliego-de-cargos"]')?.innerText?.trim() || '';
      const footer = document.querySelector('tabla-busqueda-avanzada-v3 .card')?.innerText?.trim() || '';
      const checked = document.querySelector('#' + radioId)?.checked ? groupName : 'Unknown';
      return checked + '|' + active + '|' + first + '|' + footer;
    }
    """, {"radioId": group["radio_id"], "groupName": group["name"]})


def prepare_base_page(page):
    page.goto(BASE_URL, wait_until="domcontentloaded", timeout=90000)
    page.wait_for_timeout(12000)
    close_popup(page)
    page.wait_for_timeout(2000)
    page.wait_for_selector("tabla-busqueda-avanzada-v3", timeout=60000)


def group_is_active(page, group):
    """True once the group's radio is checked AND the table already shows at
    least one row whose ESTADO starts with the group's estado_prefix, so we
    never scrape a stale table under the wrong group's label."""
    return page.evaluate("""
    ({radioId, estadoPrefix}) => {
      const norm = t => (t || '').toLowerCase().normalize('NFD').replace(/[\\u0300-\\u036f]/g, '').trim();
      if (!document.querySelector('#' + radioId)?.checked) return false;
      const rows = Array.from(document.querySelectorAll('tabla-busqueda-avanzada-v3 tbody tr'));
      return rows.some(row => {
        const cells = Array.from(row.querySelectorAll('th, td')).map(td => norm(td.innerText));
        return cells.some(c => c.startsWith(estadoPrefix));
      });
    }
    """, {"radioId": group["radio_id"], "estadoPrefix": group["estado_prefix"]})


def click_group(page, group) -> bool:
    """Switch to the given group's tab and confirm it landed. Same
    retry-then-reload pattern as 010-collect-index.py's click_status() — the
    Angular re-render regularly races a plain radio click."""
    for attempt in range(1, GROUP_SWITCH_ATTEMPTS + 1):
        if attempt == GROUP_SWITCH_ATTEMPTS and attempt > 1:
            print(f"{group['name']}: reloading page for final switch attempt")
            prepare_base_page(page)
        close_popup(page)
        page.evaluate("""
        ({radioId}) => {
          const label = document.querySelector('label[for="' + radioId + '"]');
          const input = document.querySelector('#' + radioId);
          if (label) label.click();
          else if (input) {
            input.click();
            input.dispatchEvent(new Event('change', { bubbles: true }));
          }
        }
        """, {"radioId": group["radio_id"]})
        for _ in range(40):
            page.wait_for_timeout(500)
            if group_is_active(page, group):
                page.wait_for_timeout(3500)
                close_popup(page)
                wait_for_table(page)
                return True
        print(f"{group['name']}: switch attempt {attempt}/{GROUP_SWITCH_ATTEMPTS} failed (rows never showed '{group['estado_prefix']}*')")
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


def extract_rows(page, page_number):
    return page.evaluate("""
    ({pageNumber}) => {
      function clean(text) { return (text || '').replace(/\\s+/g, ' ').trim(); }
      function absoluteUrl(href) {
        if (!href) return '';
        try { return new URL(href, window.location.href).href; } catch (e) { return href; }
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
    """, {"pageNumber": page_number})


def click_next(page, group):
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
    before = page_signature(page, group)
    page.evaluate("""
    (() => {
      const next = document.querySelector('ngb-pagination a[aria-label="Next"]');
      if (next) next.click();
    })();
    """)
    for _ in range(40):
        page.wait_for_timeout(500)
        if page_signature(page, group) != before:
            page.wait_for_timeout(2500)
            return True, "Clicked next"
    return False, "Next clicked but page did not change"


def skip_to_page(page, group, target_page: int) -> tuple[int, str]:
    """Advance from page 1 to ``target_page`` via repeated Next clicks (no
    direct page-N navigation exists in this Angular pager). Returns the page
    actually reached and a stop reason if it fell short."""
    page_number = 1
    while page_number < target_page:
        wait_for_table(page)
        moved, why = click_next(page, group)
        if not moved:
            return page_number, f"could not advance to backfill page {target_page} ({why})"
        page_number += 1
    return page_number, ""


def main():
    conn = init_db()
    mode = closed_mode()
    run_started = now_iso()

    group = GROUP

    cutoff_date = None
    range_end_date = None
    if mode == "backfill":
        state = get_closed_crawl_state(conn, grupo=group["name"])
        if state["backfill_complete"]:
            print(f"Backfill already reached the last {group['name']} page (or its "
                  "target date); nothing to do. Reset the cursor from the monitor to "
                  "re-run it, raise PC_CLOSED_BACKFILL_DAYS to go deeper, or set "
                  "PC_CLOSED_BACKFILL_START_DATE/END_DATE for a fresh date range.")
            return
        start_page = max(1, int(state["backfill_page"]))
        page_cap = backfill_page_cap()
        range_start_date = backfill_start_date()
        range_end_date = backfill_end_date()
        # A staff-supplied start date replaces the day-count cutoff outright —
        # the two are alternative ways to say "how far back to go", not
        # additive. The listing sorts newest-closed-first, so an end date
        # just means "skip rows newer than this" until paging naturally
        # reaches it; it does not change where the crawl stops.
        cutoff_date = range_start_date or backfill_cutoff_date()
    else:
        start_page = 1
        page_cap = forward_page_cap()
        cutoff_date = forward_cutoff_date()

    extracted_total = 0
    new_records = 0
    existing_records = 0
    json_written = 0
    json_skipped = 0
    seen = set()
    page_counts = []
    stop_reason = ""
    reached_last_page = False
    reached_cutoff_date = False

    with sync_playwright() as p:
        browser = p.firefox.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--window-size=1280,720"],
        )
        page = browser.new_page(viewport={"width": 1280, "height": 720})
        prepare_base_page(page)

        # Backfill must walk the complete pager and use the listing's own FECHA
        # values as the stop condition. The portal's native date filter can
        # truncate/alter the pager and report no next page before the requested
        # start date is reached, which falsely marks the cursor complete. Keep
        # the native filter for forward discovery only; range_end_date is still
        # enforced below when deciding which rows to insert.
        if cutoff_date is not None and mode != "backfill":
            if apply_native_date_filter(page, cutoff_date, datetime.now().date()):
                print(f"{group['name']}: applied native date filter {cutoff_date.isoformat()} to {datetime.now().date()}")
        elif mode == "backfill":
            print(f"{group['name']}: backfill native date filter disabled; paging until {cutoff_date.isoformat()}")

        if not click_group(page, group):
            print(f"{group['name']}: could not open the tab after {GROUP_SWITCH_ATTEMPTS} attempts; aborting this run.")
            browser.close()
            return

        set_rows_to_50(page)
        go_first_page(page)

        page_number = 1
        if start_page > 1:
            page_number, skip_stop = skip_to_page(page, group, start_page)
            if skip_stop:
                stop_reason = skip_stop
                reached_last_page = True  # ran out of pages before reaching the cursor

        pages_crawled_this_run = 0
        if not stop_reason:
            while True:
                if pages_crawled_this_run >= page_cap:
                    stop_reason = f"page cap reached ({page_cap})"
                    break

                wait_for_table(page)
                rows = extract_rows(page, page_number)
                rows = [
                    r for r in rows
                    if not r["estado"] or strip_accents(r["estado"]).lower().startswith(group["estado_prefix"])
                ]
                extracted_total += len(rows)
                page_counts.append((page_number, len(rows)))
                pages_crawled_this_run += 1

                if cutoff_date is not None:
                    page_dates = [d for d in (parse_dmy_date(r["fecha"]) for r in rows) if d]
                    # The listing sorts newest-closed-first, so once every
                    # parseable date on a page is already past the cutoff we
                    # have gone back far enough — no need to keep paging
                    # through the (much larger) remainder of the archive.
                    if page_dates and max(page_dates) < cutoff_date:
                        reached_cutoff_date = True

                for r in rows:
                    numero = r["numero"]
                    if numero in seen:
                        continue
                    seen.add(numero)

                    if range_end_date is not None:
                        row_date = parse_dmy_date(r["fecha"])
                        if row_date and row_date > range_end_date:
                            # Newer than the requested range — keep paging
                            # past it without inserting; the crawl only stops
                            # once it reaches the start-date cutoff above.
                            continue

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
                        "grupo": group["name"],
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
                        # Full-detail archiving is picked up separately by
                        # 037b-collect-closed-details.py on this feature's own
                        # low-resource schedule (030-collect-details.py's own
                        # queue excludes grupo='Closed') — a record already
                        # 'saved' from when it was still Abierta/Programada
                        # keeps that status untouched here.
                        "detail_status": (
                            existing["detail_status"] if existing
                            else "saved" if existing_on_disk and archive_complete(record_folder, numero)
                            else "pending"
                        ),
                        "finish_date_guess": existing["finish_date_guess"] if existing else "",
                        "source_page_first_seen_or_last_seen": page_number,
                        "visual_row_first_seen_or_last_seen": r.get("visual_row"),
                    }

                    result = insert_or_update_index(conn, row)
                    if result == "new" and not existing_on_disk:
                        new_records += 1
                    else:
                        existing_records += 1

                    if write_json_once(index_json_path, row):
                        json_written += 1
                    else:
                        json_skipped += 1

                print(f"{group['name']} page {page_number}: {len(rows)} rows (new={new_records}, existing={existing_records})")

                if reached_cutoff_date:
                    stop_reason = f"reached {mode} target date ({cutoff_date.isoformat()})"
                    break

                moved, reason = click_next(page, group)
                if not moved:
                    stop_reason = reason
                    reached_last_page = True
                    break
                page_number += 1

        browser.close()

    if mode == "backfill":
        update_kwargs = {"last_backfill_run_at": now_iso()}
        if reached_last_page:
            update_kwargs["backfill_complete"] = 1
            print(f"Backfill reached the last {group['name']} page — marking complete.")
        elif reached_cutoff_date:
            update_kwargs["backfill_complete"] = 1
            print(f"Backfill reached its {backfill_target_days()}-day target date — marking complete.")
        else:
            update_kwargs["backfill_page"] = page_number
        update_closed_crawl_state(conn, grupo=group["name"], **update_kwargs)
    else:
        update_closed_crawl_state(conn, grupo=group["name"], last_forward_run_at=now_iso())

    db_group_total = conn.execute(
        "SELECT COUNT(*) AS c FROM opportunities WHERE grupo = ?", (group["name"],)
    ).fetchone()["c"]

    summary_lines = [
        f"{group['name'].upper()} INDEX RUN ({mode}) started: {run_started}",
        f"{group['name'].upper()} INDEX RUN finished: {now_iso()}",
        f"Start page: {start_page}  Page cap this run: {page_cap}",
        f"Stop reason: {stop_reason or '(page cap reached)'}",
    ] + ([
        f"Date range: start={cutoff_date.isoformat() if cutoff_date else '(365-day default)'}"
        f" end={range_end_date.isoformat() if range_end_date else '(none, forward from most-recent)'}",
    ] if mode == "backfill" else [
        f"Date cutoff: {cutoff_date.isoformat()} ({forward_target_days()}-day window)",
    ]) + [
        "",
        f"Rows extracted total from site: {extracted_total}",
        f"Unique NUMERO values in this run: {len(seen)}",
        f"New DB records inserted: {new_records}",
        f"Existing DB records seen/updated: {existing_records}",
        f"Index JSON written: {json_written}",
        f"Index JSON skipped existing: {json_skipped}",
        "",
        f"DB total {group['name']} records: {db_group_total}",
        "",
        "Page counts:",
    ]
    for page_num, qty in page_counts:
        summary_lines.append(f"  page {page_num}: {qty} rows")
    summary = "\n".join(summary_lines) + "\n"

    log_path = LOG_DIR / f"{group['name'].lower()}_index_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    log_path.write_text(summary, encoding="utf-8")
    print(summary)


if __name__ == "__main__":
    main()
