(async () => {
  const CONFIG = {
    // Own output element/title so this watch's snapshot is a distinct diff
    // target from the Abiertas/Programadas watch (changedetection-browser-
    // steps.js) — the two run as separate changedetection.io watches with
    // separate webhook notification URLs (see 070-run-collector-cerradas-new.sh
    // / src/10_webhook/010-webhook-listener.py's "panamacompra-cerradas"
    // route), so a change here can never trigger the other pipeline.
    outputSelector: "#pc-monitor-output-cerradas",

    monitorTitle: "PANAMACOMPRA_MONITOR_CERRADAS",
    urlLabel: "cotizaciones-en-linea",

    rowsPerPage: "50",
    // Small on purpose: this watch only needs to notice that NEW closures
    // appeared (they always show up near the front, same assumption
    // forward_page_cap() makes in 037-collect-cerradas-index.py) — it is not
    // the data source for Cerradas the way the Abiertas/Programadas watch is
    // for those groups. The full historical archive is crawled separately
    // and incrementally by 039-run-cerradas-backfill.sh, not by this watch.
    maxPagesSafety: 6,
    switchAttempts: 3,
    retryBackoffMs: 4000,

    initialWaitMs: 8000,
    afterClickWaitMs: 1300,
    afterRowsChangeWaitMs: 4000,
    afterNextPageWaitMs: 3500,
    waitMs: 25000,

    statuses: [
      {
        group: "Cerradas",
        radioId: "btnradio3",
        labelText: "Cerradas",
        expectedEstado: "Cerrada",
        totalLabel: "Cerradas collected"
      }
    ],

    fields: [
      ["numero", "NUMERO"],
      ["estado", "ESTADO"],
      ["descripcion", "DESCRIPCION"],
      ["entidad", "ENTIDAD"],
      ["dependencia", "DEPENDENCIA"],
      ["fecha", "FECHA"],
      ["modalidad", "MODALIDAD"],
      ["link", "LINK"]
    ]
  };

  const sleep = (ms) => new Promise(resolve => setTimeout(resolve, ms));

  const clean = (value) =>
    String(value || "")
      .replace(/\s+/g, " ")
      .trim();

  // Strips NFD combining diacritical marks (U+0300-U+036F) by codepoint
  // instead of a regex character class, to avoid any ambiguity from
  // embedding literal combining characters in source.
  const normalize = (value) =>
    clean(value)
      .toLowerCase()
      .normalize("NFD")
      .split("")
      .filter((ch) => {
        const code = ch.codePointAt(0);
        return !(code >= 0x0300 && code <= 0x036f);
      })
      .join("");

  function ensureOutput() {
    const outputId = CONFIG.outputSelector.replace(/^#/, "");
    let output = document.querySelector(CONFIG.outputSelector);

    if (!output) {
      output = document.createElement("pre");
      output.id = outputId;
      output.setAttribute("data-pc-monitor", "1");
      document.body.prepend(output);
    }

    output.style.whiteSpace = "pre-wrap";
    output.style.fontFamily = "monospace";
    output.style.fontSize = "12px";
    output.style.padding = "20px";
    output.style.margin = "0";
    output.style.background = "#fff";
    output.style.color = "#000";
    output.style.border = "1px solid #ddd";

    return output;
  }

  function writeOutput(text) {
    const output = ensureOutput();
    output.textContent = String(text || "").trim() || "NO_TABLE_VALUES_FOUND";
  }

  function isVisible(el) {
    if (!el) return false;

    try {
      const rect = el.getBoundingClientRect();
      const style = window.getComputedStyle(el);

      return (
        rect.width > 0 &&
        rect.height > 0 &&
        style.visibility !== "hidden" &&
        style.display !== "none"
      );
    } catch (_) {
      return false;
    }
  }

  function dispatchBasicEvents(el) {
    for (const type of ["input", "change"]) {
      try {
        el.dispatchEvent(new Event(type, { bubbles: true, cancelable: true }));
      } catch (_) {}
    }
  }

  async function humanClick(el, waitMs = CONFIG.afterClickWaitMs) {
    if (!el) return false;

    try {
      el.scrollIntoView({ block: "center", inline: "center" });
    } catch (_) {}

    await sleep(200);

    const opts = {
      bubbles: true,
      cancelable: true,
      view: window
    };

    for (const type of ["mouseover", "mouseenter", "mousedown", "mouseup", "click"]) {
      try {
        el.dispatchEvent(new MouseEvent(type, opts));
      } catch (_) {}
    }

    try {
      el.click();
    } catch (_) {}

    await sleep(waitMs);
    return true;
  }

  async function closePopup() {
    const selectors = [
      "ngb-modal-window button.btn-close",
      "ngb-modal-window button[aria-label='Close']",
      "ngb-modal-window button[aria-label='Cerrar']",
      ".modal button.btn-close",
      ".modal button[aria-label='Close']",
      ".modal button[aria-label='Cerrar']",
      "button.btn-close",
      "[data-bs-dismiss='modal']"
    ];

    for (const selector of selectors) {
      const btn = document.querySelector(selector);

      if (btn && isVisible(btn)) {
        await humanClick(btn, 700);
      }
    }

    const closeByText = [...document.querySelectorAll("button, a")]
      .find(el => {
        const txt = normalize(el.innerText || el.textContent);
        return isVisible(el) && /cerrar|aceptar|continuar|entendido|omitir/.test(txt);
      });

    if (closeByText) {
      await humanClick(closeByText, 700);
    }

    document.querySelectorAll("ngb-modal-window, .modal-backdrop, .cdk-overlay-container")
      .forEach(el => el.remove());

    document.body.classList.remove("modal-open");
    document.body.style.overflow = "auto";
    document.body.style.paddingRight = "";
  }

  function getTable() {
    return (
      document.querySelector("tabla-busqueda-avanzada-v3 table") ||
      document.querySelector(".table-responsive table") ||
      document.querySelector("table.table") ||
      document.querySelector("table")
    );
  }

  function getHeaderMap(table) {
    const headers = [...table.querySelectorAll("thead th")]
      .map(th => normalize(th.innerText || th.textContent));

    const map = {};

    headers.forEach((header, index) => {
      if (header === "numero") map.numero = index;
      if (header === "estado") map.estado = index;
      if (header === "descripcion") map.descripcion = index;
      if (header.includes("entidad")) map.entidad = index;
      if (header === "dependencia") map.dependencia = index;
      if (header === "fecha") map.fecha = index;
      if (header.includes("modalidad")) map.modalidad = index;
    });

    return map;
  }

  function getCurrentRowsRaw() {
    const table = getTable();

    if (!table) return [];

    const map = getHeaderMap(table);

    return [...table.querySelectorAll("tbody tr")]
      .map(row => {
        const cellEls = [...row.querySelectorAll("th, td")];
        const cells = cellEls.map(cell => clean(cell.innerText || cell.textContent));

        const numeroPattern = /20\d{2}-\d+-\d+-\d+-\d+-[A-Z]+-\d+/;
        const fallbackNumeroIndex = cells.findIndex(cell => numeroPattern.test(cell));
        const numeroIndex = map.numero !== undefined ? map.numero : fallbackNumeroIndex;

        if (numeroIndex < 0) return null;

        const numero = (cells[numeroIndex] || row.innerText || "").match(numeroPattern)?.[0] || "";
        const detailLink = [...row.querySelectorAll("a[href]")]
          .find(a => /solicitud-de-cotizacion|pliego-de-cargos/.test(a.getAttribute("href") || ""));

        if (!numero) return null;

        return {
          numero,
          estado: map.estado !== undefined ? cells[map.estado] || "" : "",
          descripcion: map.descripcion !== undefined ? cells[map.descripcion] || "" : "",
          entidad: map.entidad !== undefined ? cells[map.entidad] || "" : "",
          dependencia: map.dependencia !== undefined ? cells[map.dependencia] || "" : "",
          fecha: map.fecha !== undefined ? cells[map.fecha] || "" : "",
          modalidad: map.modalidad !== undefined ? cells[map.modalidad] || "" : "",
          link: detailLink?.href || ""
        };
      })
      .filter(Boolean);
  }

  function hasRowsWithEstado(expectedEstado) {
    // startsWith instead of strict equality: tolerates portal suffixes such as
    // "Cerrada " variations without ever accepting another group's estado.
    const wanted = normalize(expectedEstado);

    return getCurrentRowsRaw()
      .some(row => normalize(row.estado).startsWith(wanted));
  }

  async function waitFor(checkFn, maxMs = CONFIG.waitMs) {
    const started = Date.now();

    while (Date.now() - started < maxMs) {
      try {
        if (checkFn()) return true;
      } catch (_) {}

      await sleep(700);
    }

    try {
      return !!checkFn();
    } catch (_) {
      return false;
    }
  }

  function getExpectedTotal() {
    // The portal footer near the table/pagination shows the total record count
    // (e.g. "Mostrando 1 - 50 de 123"). 0 when it cannot be found.
    const scopes = [
      document.querySelector("tabla-busqueda-avanzada-v3 .card"),
      document.querySelector("tabla-busqueda-avanzada-v3"),
      document.querySelector("ngb-pagination")?.parentElement
    ].filter(Boolean);

    for (const el of scopes) {
      const text = clean(el.innerText || el.textContent || "");
      const match = text.match(/de\s+([\d.,]+)\s*(?:registros|resultados|entradas)?/i);

      if (match) {
        const total = parseInt(match[1].replace(/[.,]/g, ""), 10);
        if (Number.isFinite(total) && total > 0) return total;
      }
    }

    return 0;
  }

  function getPageSignature() {
    const table = getTable();

    if (!table) return "";

    return [...table.querySelectorAll("tbody tr")]
      .slice(0, 5)
      .map(row => clean(row.innerText || row.textContent))
      .join(" || ");
  }

  async function clickExactRadioStatus(statusConfig) {
    await closePopup();

    const input = document.getElementById(statusConfig.radioId);

    // Already on the right tab with matching rows: do not toggle away. Clicking
    // an already-active radio re-renders the table and can race us into the
    // "rows not ready" state for no reason.
    if (input && input.checked && hasRowsWithEstado(statusConfig.expectedEstado)) {
      return true;
    }

    const label =
      document.querySelector(`label[for="${statusConfig.radioId}"]`) ||
      [...document.querySelectorAll("label")]
        .find(el => normalize(el.innerText || el.textContent).includes(normalize(statusConfig.labelText)));

    if (label) {
      await humanClick(label, 1200);
    }

    if (input) {
      try {
        input.checked = true;
        dispatchBasicEvents(input);
      } catch (_) {}

      await humanClick(input, 800);
    }

    if (label) {
      await humanClick(label, 1200);
    }

    await closePopup();

    return await waitFor(
      () => hasRowsWithEstado(statusConfig.expectedEstado),
      CONFIG.waitMs
    );
  }

  async function setRowsPerPage50() {
    const selects = [...document.querySelectorAll("select")]
      .filter(select => {
        const options = [...select.options].map(opt => clean(opt.textContent));
        return options.includes(CONFIG.rowsPerPage);
      });

    for (const select of selects) {
      const option = [...select.options]
        .find(opt => clean(opt.textContent) === CONFIG.rowsPerPage);

      if (!option) continue;

      try {
        select.value = option.value;
        select.selectedIndex = [...select.options].indexOf(option);
        dispatchBasicEvents(select);
      } catch (_) {}

      await sleep(CONFIG.afterRowsChangeWaitMs);
      return true;
    }

    return false;
  }

  async function goFirstPageIfPossible() {
    const first =
      document.querySelector("ngb-pagination a[aria-label='First'], a[aria-label='First']") ||
      [...document.querySelectorAll("ngb-pagination a.page-link, a.page-link")]
        .find(a => clean(a.innerText || a.textContent) === "1");

    if (!first || !isVisible(first)) return false;

    const parent = first.closest("li");

    const disabled =
      first.getAttribute("aria-disabled") === "true" ||
      first.hasAttribute("disabled") ||
      parent?.classList.contains("disabled") ||
      parent?.classList.contains("active");

    if (disabled) return false;

    const before = getPageSignature();

    await humanClick(first, CONFIG.afterNextPageWaitMs);

    const after = getPageSignature();

    return before !== after;
  }

  async function goNextPage() {
    const next = document.querySelector(
      "ngb-pagination a[aria-label='Next'], a[aria-label='Next']"
    );

    if (!next || !isVisible(next)) return { moved: false, complete: true, reason: "Next not found" };

    const parent = next.closest("li");

    const disabled =
      next.getAttribute("aria-disabled") === "true" ||
      next.hasAttribute("disabled") ||
      parent?.classList.contains("disabled");

    if (disabled) return { moved: false, complete: true, reason: "Next disabled" };

    const before = getPageSignature();

    await humanClick(next, CONFIG.afterNextPageWaitMs);

    const after = getPageSignature();

    return before !== after
      ? { moved: true, complete: false, reason: "" }
      : { moved: false, complete: false, reason: "Next clicked but page did not change" };
  }

  function extractOnlyExpectedRows(statusConfig) {
    const wanted = normalize(statusConfig.expectedEstado);

    return getCurrentRowsRaw()
      .filter(row => normalize(row.estado).startsWith(wanted));
  }

  async function crawlStatus(statusConfig) {
    const pageCounts = [];
    const recordsByNumero = new Map();
    const recordsInCrawlOrder = [];
    let duplicateCount = 0;
    let complete = false;

    let ready = false;
    let failReason = "";

    for (let attempt = 1; attempt <= CONFIG.switchAttempts; attempt++) {
      const switched = await clickExactRadioStatus(statusConfig);

      if (!switched) {
        failReason = `radio switch failed (attempt ${attempt}/${CONFIG.switchAttempts})`;
        await closePopup();
        await sleep(CONFIG.retryBackoffMs);
        continue;
      }

      await setRowsPerPage50();

      ready = await waitFor(
        () => hasRowsWithEstado(statusConfig.expectedEstado),
        CONFIG.waitMs
      );

      if (ready) break;

      failReason = `rows not ready after 50 change (attempt ${attempt}/${CONFIG.switchAttempts})`;
      await closePopup();
      await sleep(CONFIG.retryBackoffMs);
    }

    const rowsPerPage = parseInt(CONFIG.rowsPerPage, 10) || 50;

    function paginationLine(expectedItems, crawledPages, firstBadPage) {
      const expectedPages = expectedItems > 0 ? Math.ceil(expectedItems / rowsPerPage) : crawledPages;
      const crawledItems = recordsInCrawlOrder.length;
      const consistent =
        complete &&
        firstBadPage === 0 &&
        (expectedItems === 0 || crawledItems + duplicateCount >= expectedItems);

      return `${statusConfig.group} PAGINATION: expected_items=${expectedItems} expected_pages=${expectedPages} ` +
        `rows_per_page=${rowsPerPage} crawled_items=${crawledItems} crawled_pages=${crawledPages} ` +
        `first_bad_page=${consistent ? 0 : (firstBadPage || crawledPages + 1)} consistent=${consistent ? "yes" : "no"}`;
    }

    if (!ready) {
      pageCounts.push(paginationLine(0, 0, 1));
      pageCounts.push(`${statusConfig.group}: ${failReason || "never became ready"}`);
      return {
        config: statusConfig,
        records: [],
        recordsInCrawlOrder: [],
        pageCounts,
        duplicateCount,
        complete: false
      };
    }

    await goFirstPageIfPossible();

    await waitFor(
      () => hasRowsWithEstado(statusConfig.expectedEstado),
      CONFIG.waitMs
    );

    const expectedItems = getExpectedTotal();
    let crawledPages = 0;
    let firstBadPage = 0;

    const markBadPage = (page) => {
      if (!firstBadPage) firstBadPage = page;
    };

    // Note: expectedRows.length < rowsPerPage is NOT treated as "bad page"
    // here — unlike the Abiertas/Programadas watch, this one deliberately
    // stops at maxPagesSafety long before the real last page (Cerradas has
    // far more history than this watch needs to see), so a short-looking
    // page is expected, not a sign of dropped rows.
    for (let page = 1; page <= CONFIG.maxPagesSafety; page++) {
      const ready = await waitFor(
        () => hasRowsWithEstado(statusConfig.expectedEstado),
        CONFIG.waitMs
      );

      if (!ready) {
        markBadPage(page);
        pageCounts.push(`${statusConfig.group}: page ${page} never became ready`);
        break;
      }

      crawledPages = page;

      const expectedRows = extractOnlyExpectedRows(statusConfig);
      const rows = expectedRows.filter(row => row.numero && row.link);

      pageCounts.push(`${statusConfig.group} page ${page}: ${rows.length}`);

      if (rows.length !== expectedRows.length) {
        markBadPage(page);
        pageCounts.push(`${statusConfig.group}: page ${page} has ${expectedRows.length - rows.length} row(s) without a usable NUMERO/detail link`);
      }

      for (const row of rows) {
        if (!row.numero) continue;

        if (!recordsByNumero.has(row.numero)) {
          recordsByNumero.set(row.numero, row);
          recordsInCrawlOrder.push(row);
        } else {
          duplicateCount++;
        }
      }

      const next = await goNextPage();

      if (!next.moved) {
        complete = next.complete && rows.length === expectedRows.length;
        pageCounts.push(complete
          ? `${statusConfig.group} COMPLETE: ${next.reason}`
          : `${statusConfig.group}: reached the real last page early (${next.reason || "pagination ended"})`);
        break;
      }
    }

    if (!complete && crawledPages >= CONFIG.maxPagesSafety &&
        !pageCounts.some(line => line.startsWith(`${statusConfig.group}:`))) {
      // Expected outcome for this watch: it intentionally only samples the
      // first maxPagesSafety pages to detect new closures, not the whole
      // archive, so hitting the cap here is success, not a health problem.
      pageCounts.push(`${statusConfig.group}: sampled first ${CONFIG.maxPagesSafety} page(s) (by design, not the full archive)`);
    }

    pageCounts.unshift(paginationLine(expectedItems, crawledPages, firstBadPage));

    return {
      config: statusConfig,
      records: [...recordsByNumero.values()],
      recordsInCrawlOrder,
      pageCounts,
      duplicateCount,
      complete
    };
  }

  function renderRecord(row) {
    return CONFIG.fields
      .map(([property, label]) => `${label}: ${clean(row[property] || "")}`)
      .join("\n");
  }

  function mergeResultsInCrawlOrder(resultSets) {
    const allRecordsByNumero = new Map();
    const uniqueRecordsInCrawlOrder = [];
    let duplicateCount = resultSets.reduce((total, resultSet) => total + (resultSet.duplicateCount || 0), 0);

    for (const resultSet of resultSets) {
      for (const row of resultSet.recordsInCrawlOrder) {
        if (!row.numero) continue;

        if (allRecordsByNumero.has(row.numero)) {
          duplicateCount++;
          continue;
        }

        allRecordsByNumero.set(row.numero, row);
        uniqueRecordsInCrawlOrder.push(row);
      }
    }

    return {
      allRecordsByNumero,
      uniqueRecordsInCrawlOrder,
      duplicateCount
    };
  }

  function buildOutput(resultSets, merged) {
    const totalLines = resultSets.map(resultSet =>
      `${resultSet.config.totalLabel}: ${resultSet.records.length}`
    );

    const pageCountLines = resultSets.flatMap(resultSet => resultSet.pageCounts);

    const recordBlocks = merged.uniqueRecordsInCrawlOrder.map(renderRecord);

    return [
      CONFIG.monitorTitle,
      `URL: ${CONFIG.urlLabel}`,
      `Unique NUMERO collected: ${merged.allRecordsByNumero.size}`,
      ...totalLines,
      `Duplicate NUMERO skipped: ${merged.duplicateCount}`,
      "",
      "PAGE_COUNTS",
      ...pageCountLines,
      "",
      "RECORDS",
      ...recordBlocks.map(block => `${block}\n---`)
    ].join("\n");
  }

  ensureOutput();
  writeOutput("WAITING_FOR_TABLE_VALUES");

  try {
    await sleep(CONFIG.initialWaitMs);
    await closePopup();

    const resultSets = [];

    for (const statusConfig of CONFIG.statuses) {
      const result = await crawlStatus(statusConfig);
      resultSets.push(result);
    }

    const merged = mergeResultsInCrawlOrder(resultSets);

    writeOutput(
      buildOutput(resultSets, merged)
    );
  } catch (err) {
    writeOutput(`SCRIPT_ERROR: ${clean(err && err.message ? err.message : err)}`);
  }
})();
