#!/usr/bin/env python3
"""Low-resource background collector for the 'cuadro de cotizaciones' price/
provider comparison page a closed (Cerrada) opportunity links to — the actual
value of the Cerradas feature: for each item bid on, who quoted what price,
so cotizacion_price_stats() (common.py) can report min/avg/max per item.

Queue: opportunities where grupo='Cerradas' and cotizacion_status is not yet
a terminal state ('saved', 'no_bids', 'no_link'), capped by
PC_CERRADAS_DETAIL_LIMIT per run (default small — this opens two pages per
record: the solicitud-de-cotizacion detail page, only when cuadro_link isn't
already cached, then the cuadro page itself).

Structure discovered by live inspection of the real site (verified against
several real closed opportunities, not guessed):
  detail page  -> an <a> whose text is "Ver documento" and whose href
                  contains "cuadro-de-cotizaciones" (present only when the
                  opportunity actually has that document).
  cuadro page  -> first <table> is the summary (Número/Descripción/Entidad/
                  Unidad de Compra/Modalidad/Proponentes participante).
                  Every following <table> is ONE bidder's full item list, its
                  provider name in <caption><a>NAME</a></caption>, item rows
                  under the usual #/Descripción/.../Precio Unitario/Monto
                  Neto/Impuestos header set — mapped by header text, not
                  column position, so a reordered column can't silently
                  scramble the data.
"""
import json
import os
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from common import *

MAX_COTIZACION_ATTEMPTS = env_int("PC_CERRADAS_DETAIL_MAX_ATTEMPTS", "3", minimum=1)


def cotizacion_detail_limit() -> int:
    return env_int("PC_CERRADAS_DETAIL_LIMIT", "10", minimum=1)


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


def cotizacion_pending_rows(conn, limit: int, max_attempts: int):
    rows = conn.execute("""
    SELECT * FROM opportunities
    WHERE grupo = 'Cerradas'
      AND COALESCE(cotizacion_status, '') NOT IN ('saved', 'no_bids', 'no_link')
      AND cotizacion_attempts < ?
    ORDER BY cotizacion_attempts ASC, first_seen ASC
    """, (max_attempts,)).fetchall()
    return rows[:limit] if limit > 0 else rows


def find_cuadro_link(page) -> str:
    """The 'Ver documento' link's href on a solicitud-de-cotizacion detail
    page, absolute-URL'd, or '' if this closed opportunity has none (e.g.
    cancelled/no submissions ever reached the cotización stage)."""
    return page.evaluate("""
    (() => {
      const anchors = Array.from(document.querySelectorAll('a[href*="cuadro-de-cotizaciones"]'));
      const a = anchors.find(el => (el.textContent || '').trim().length > 0) || anchors[0];
      if (!a) return '';
      try { return new URL(a.getAttribute('href'), window.location.href).href; }
      catch (e) { return a.href || ''; }
    })();
    """) or ""


def extract_cuadro(page) -> dict:
    """Structured payload for the whole cuadro page — see module docstring
    for the confirmed DOM shape. providers[].items[] entries use the same
    field names as the cotizacion_bids DB columns (minus numero/proponente,
    added by the caller) so save_cotizacion_bids() can consume them directly."""
    return page.evaluate(r"""
    (() => {
      function clean(t) { return (t || '').replace(/\s+/g, ' ').trim(); }

      const tables = Array.from(document.querySelectorAll('table'));
      if (tables.length === 0) return { summary: {}, providers: [] };

      const summary = {};
      Array.from(tables[0].querySelectorAll('tbody tr, tr')).forEach(tr => {
        const cells = Array.from(tr.querySelectorAll('th, td')).map(c => clean(c.innerText));
        if (cells.length >= 2 && cells[0]) summary[cells[0]] = cells[1];
      });

      // Substring keys (not exact match) against the lowercased header text —
      // deliberately avoids any accent-stripping/Unicode-range regex, which
      // is easy to get subtly wrong; these substrings are plain ASCII and
      // match the real column headers confirmed by live inspection
      // ("Descripción del Bien/Servicio/Obra", "Cantidad Cotización", etc. —
      // "descripcion"/"cotizacion" without the accent still appear as a
      // substring of the accented header text after lowercasing, since
      // toLowerCase() does not strip diacritics but also does not need to
      // here: JS string.includes matches the base run of ASCII letters
      // around the accented character fine either way for these headers).
      const HEADER_RULES = [
        ['bien/servicio/obra', 'item_descripcion'],
        ['especificaciones del comprador', 'especificaciones_comprador'],
        ['cantidad solicitada', 'cantidad_solicitada'],
        ['unidad de medida', 'unidad_medida'],
        ['especificaciones del proponente', 'especificaciones_proponente'],
        ['cantidad cotiza', 'cantidad_cotizada'],
        ['precio unitario', 'precio_unitario'],
        ['monto neto', 'monto_neto'],
        ['impuestos', 'impuestos'],
      ];
      function headerField(t) {
        const norm = clean(t).toLowerCase();
        const hit = HEADER_RULES.find(([needle]) => norm.includes(needle));
        return hit ? hit[1] : null;
      }

      const providers = [];
      for (let i = 1; i < tables.length; i++) {
        const table = tables[i];
        const nameEl = table.querySelector('caption a');
        const name = nameEl ? clean(nameEl.textContent) : '';
        if (!name) continue;

        const headerCells = Array.from(table.querySelectorAll('thead th'));
        const fieldByCol = {};
        headerCells.forEach((th, idx) => {
          const key = headerField(th.textContent);
          if (key) fieldByCol[idx] = key;
        });

        const items = [];
        Array.from(table.querySelectorAll('tbody tr')).forEach((tr, rowIdx) => {
          const cells = Array.from(tr.querySelectorAll('th, td'));
          if (cells.length === 0) return;
          // Sub Total / Impuestos / Total footer rows have far fewer cells
          // than the header — skip anything that isn't a real item row.
          if (cells.length < headerCells.length - 2) return;
          const item = { item_index: rowIdx + 1 };
          cells.forEach((cell, idx) => {
            const field = fieldByCol[idx];
            if (field) item[field] = clean(cell.innerText);
          });
          if (item.item_descripcion) items.push(item);
        });

        providers.push({ name, items });
      }

      return { summary, providers };
    })();
    """)


def main():
    conn = init_db()
    run_started = now_iso()
    limit = cotizacion_detail_limit()
    rows = cotizacion_pending_rows(conn, limit, MAX_COTIZACION_ATTEMPTS)

    saved = 0
    no_bids = 0
    no_link = 0
    failed = 0

    if not rows:
        print("No Cerradas records pending a cotizacion fetch.")
        return

    with sync_playwright() as p:
        browser = p.firefox.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--window-size=1280,900"],
        )
        page = browser.new_page(viewport={"width": 1280, "height": 900})

        for row in rows:
            numero = row["numero"]
            conn.execute(
                "UPDATE opportunities SET cotizacion_attempts = cotizacion_attempts + 1 WHERE numero = ?",
                (numero,),
            )
            conn.commit()

            try:
                cuadro_url = row["cuadro_link"] or ""
                if not cuadro_url:
                    page.goto(row["link"], wait_until="domcontentloaded", timeout=60000)
                    page.wait_for_timeout(5000)
                    close_popup(page)
                    cuadro_url = find_cuadro_link(page)
                    if not cuadro_url:
                        conn.execute(
                            "UPDATE opportunities SET cotizacion_status = 'no_link' WHERE numero = ?",
                            (numero,),
                        )
                        conn.commit()
                        no_link += 1
                        print(f"{numero}: no cuadro de cotizaciones link found")
                        continue

                page.goto(cuadro_url, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(5000)
                close_popup(page)
                page.wait_for_timeout(1000)

                data = extract_cuadro(page)
                providers = data.get("providers") or []

                if not providers:
                    conn.execute(
                        "UPDATE opportunities SET cotizacion_status = 'no_bids', cuadro_link = ? WHERE numero = ?",
                        (cuadro_url, numero),
                    )
                    conn.commit()
                    no_bids += 1
                    print(f"{numero}: cuadro found, 0 proponentes with items")
                    continue

                bids = []
                for provider in providers:
                    for item in provider.get("items") or []:
                        bids.append({
                            "item_index": item.get("item_index"),
                            "item_descripcion": item.get("item_descripcion"),
                            "especificaciones_comprador": item.get("especificaciones_comprador"),
                            "cantidad_solicitada": item.get("cantidad_solicitada"),
                            "unidad_medida": item.get("unidad_medida"),
                            "proponente": provider["name"],
                            "especificaciones_proponente": item.get("especificaciones_proponente"),
                            "cantidad_cotizada": item.get("cantidad_cotizada"),
                            "precio_unitario": parse_money(item.get("precio_unitario")),
                            "monto_neto": parse_money(item.get("monto_neto")),
                            "impuestos": item.get("impuestos"),
                        })

                save_cotizacion_bids(conn, numero, bids)

                json_path = Path(row["record_folder"]) / f"{safe_name(numero)}.cotizacion.json"
                json_payload = {
                    "numero": numero,
                    "cuadro_link": cuadro_url,
                    "summary": data.get("summary") or {},
                    "proponentes_count": len(providers),
                    "bids_count": len(bids),
                    "collected_at": now_iso(),
                }
                # Overwritten on every successful (re-)collection, unlike the
                # index JSON's write-once convention — save_cotizacion_bids()
                # above already replaces this record's DB rows wholesale on a
                # retry, so the on-disk mirror should match, not keep stale
                # data from a first attempt that later failed partway.
                json_tmp = json_path.with_suffix(json_path.suffix + ".tmp")
                json_tmp.write_text(json.dumps(json_payload, ensure_ascii=False, indent=2), encoding="utf-8")
                json_tmp.replace(json_path)

                conn.execute(
                    "UPDATE opportunities SET cotizacion_status = 'saved', cotizacion_saved_at = ?, "
                    "cotizacion_json_path = ?, cuadro_link = ? WHERE numero = ?",
                    (now_iso(), str(json_path), cuadro_url, numero),
                )
                conn.commit()
                saved += 1
                print(f"{numero}: saved {len(bids)} bids from {len(providers)} proponentes")

            except Exception as exc:  # noqa: BLE001 - never let one bad record stop the run
                failed += 1
                print(f"{numero}: FAILED — {exc}", file=sys.stderr)

        browser.close()

    summary = (
        f"CERRADAS COTIZACION RUN started: {run_started}\n"
        f"CERRADAS COTIZACION RUN finished: {now_iso()}\n"
        f"Queue size this run: {len(rows)} (limit={limit})\n"
        f"Saved: {saved}  No bids: {no_bids}  No cuadro link: {no_link}  Failed: {failed}\n"
    )
    log_path = LOG_DIR / f"cerradas_cotizacion_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    log_path.write_text(summary, encoding="utf-8")
    print(summary)


if __name__ == "__main__":
    main()
