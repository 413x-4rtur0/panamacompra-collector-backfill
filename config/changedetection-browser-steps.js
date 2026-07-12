(async () => {
  const CONFIG = {
    outputSelector: "#pc-monitor-output",

    monitorTitle: "PANAMACOMPRA_MONITOR",
    urlLabel: "cotizaciones-en-linea",

    rowsPerPage: "50",
    maxPagesSafety: 80,
    switchAttempts: 3,
    retryBackoffMs: 4000,

    initialWaitMs: 8000,
    afterClickWaitMs: 1300,
    afterRowsChangeWaitMs: 4000,
    afterNextPageWaitMs: 3500,
    waitMs: 25000,

    // Order matters: first Programadas, then Abiertas.
    statuses: [
      {
        group: "Programadas",
        radioId: "btnradio2",
        labelText: "Programadas",
        expectedEstado: "Programada",
        totalLabel: "Programadas collected"
      },
      {
        group: "Abiertas",
        radioId: "btnradio1",
        labelText: "Abiertas",
        expectedEstado: "Abierta",
        totalLabel: "Abiertas collected"
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

  const normalize = (value) =>
    clean(value)
      .toLowerCase()
      .normalize("NFD")
      .replace(/[\u0300-\u036f]/g, "");

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
    const wanted = normalize(expectedEstado);

    return getCurrentRowsRaw()
      .some(row => normalize(row.estado) === wanted);
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
      .filter(row => normalize(row.estado) === wanted);
  }

  async function crawlStatus(statusConfig) {
    const pageCounts = [];
    const recordsByNumero = new Map();
    const recordsInCrawlOrder = [];
    let duplicateCount = 0;
    let complete = false;

    // The radio switch and the Angular table re-render race each other, and
    // Abiertas (the second status crawled) loses regularly. Retry the whole
    // switch + rows-per-page sequence instead of giving up on first failure.
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

    if (!ready) {
      pageCounts.push(`${statusConfig.group}: ${failReason || "never became ready"}`);
      return {
        config: statusConfig,
        records: [],
        recordsInCrawlOrder: [],
        pageCounts
      };
    }

    await goFirstPageIfPossible();

    await waitFor(
      () => hasRowsWithEstado(statusConfig.expectedEstado),
      CONFIG.waitMs
    );

    for (let page = 1; page <= CONFIG.maxPagesSafety; page++) {
      const ready = await waitFor(
        () => hasRowsWithEstado(statusConfig.expectedEstado),
        CONFIG.waitMs
      );

      if (!ready) {
        pageCounts.push(`${statusConfig.group}: page ${page} never became ready`);
        break;
      }

      const expectedRows = extractOnlyExpectedRows(statusConfig);
      const rows = expectedRows.filter(row => row.numero && row.link);

      pageCounts.push(`${statusConfig.group} page ${page}: ${rows.length}`);

      if (rows.length !== expectedRows.length) {
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
          : `${statusConfig.group}: ${next.reason || "pagination incomplete"}`);
        break;
      }
    }

    if (!complete && pageCounts.length && !pageCounts.some(line => line.startsWith(`${statusConfig.group}:`))) {
      pageCounts.push(`${statusConfig.group}: safety page limit ${CONFIG.maxPagesSafety} reached`);
    }

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
