"use strict";

// ---------------------------------------------------------------------
// State
// ---------------------------------------------------------------------

const state = {
  paths: { a: "", b: "", dest: "" },
  mode: "compare", // "compare" | "dedupe"
  dedupe: {
    groups: [],       // [{digest, size, wasted_bytes, members: [ReportRow-shaped dicts]}]
    emptyGroup: null,
    keepRules: [],    // ordered list of folder rel_paths, highest priority first
    kept: {},         // digest -> rel_path, from the last /api/dedupe/resolve call
    overrides: {},    // digest -> rel_path, user's manual radio picks that beat `kept`
  },
  browseTarget: null,
  browseCurrentPath: "",
  noisy: [],
  rules: {},
  jobId: null,
  rows: [],
  errors: [],
  stats: null,
  selected: new Set(),
  sortKey: "rel_path",
  sortDir: "asc",
  filterText: "",
  statusFilters: new Set(["exclusive", "internal_copy", "unreadable"]),
  filteredRows: [],
  scrollPending: false,
  busy: false, // a request-driven action is running
  browseBusy: false, // a folder-browse request is running
  ioWorkers: 1,
  useCache: true,
  workersManuallySet: false,
  cacheHits: 0,
  cacheMisses: 0,
};

const pathInputs = {
  a: document.getElementById("input-a"),
  b: document.getElementById("input-b"),
  dest: document.getElementById("input-dest"),
};

// ---------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------

function escapeHtml(value) {
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function formatBytes(n) {
  if (n < 1024) return `${n} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let value = n / 1024;
  let unitIndex = 0;
  while (value >= 1024 && unitIndex < units.length - 1) {
    value /= 1024;
    unitIndex += 1;
  }
  return `${value.toFixed(1)} ${units[unitIndex]}`;
}

function formatDuration(seconds) {
  if (seconds < 60) return "less than a minute";
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `about ${minutes} minute${minutes !== 1 ? "s" : ""}`;
  const hours = Math.floor(minutes / 60);
  const remMinutes = minutes % 60;
  return remMinutes === 0 ? `about ${hours} h` : `about ${hours} h ${remMinutes} min`;
}

function formatThroughput(bps) {
  return `${formatBytes(bps)}/s`;
}

function statusLabel(status) {
  switch (status) {
    case "exclusive":
      return "Exclusive to A";
    case "internal_copy":
      return "Internal copy";
    case "unreadable":
      return "Unreadable";
    case "present_in_b":
      return "Present in B";
    default:
      return status;
  }
}

async function api(path, method, body) {
  const opts = { method };
  if (body !== undefined) {
    opts.headers = { "Content-Type": "application/json" };
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(path, opts);
  const data = await res.json();
  if (!res.ok) {
    throw new Error((data && data.error) || `request failed with status ${res.status}`);
  }
  return data;
}

function showScreen(id) {
  document.querySelectorAll(".screen").forEach((el) => el.classList.remove("active"));
  document.getElementById(id).classList.add("active");

  // The path summary is redundant with the inputs on screen 1, so it only
  // shows on the screens that follow folder selection.
  document.getElementById("path-summary").hidden = id === "screen-select";
}

function updatePathSummary() {
  document.getElementById("summary-path-a").textContent = state.paths.a;
  document.getElementById("summary-path-dest").textContent = state.paths.dest;

  // Folder B is stale in dedupe mode (see loadVolumeInfo()'s comment for why),
  // so its line in the summary is hidden rather than showing a path that will
  // never actually be scanned.
  const isDedupe = state.mode === "dedupe";
  document.getElementById("summary-row-b").hidden = isDedupe;
  if (!isDedupe) {
    document.getElementById("summary-path-b").textContent = state.paths.b;
  }
}

/**
 * Run an action that talks to the server, with the button disabled throughout.
 *
 * Guards against the impatient double-click: the button is disabled before the
 * first await, and a global flag blocks any other action from starting in
 * parallel. The button is restored in `finally` so a failed request never
 * leaves the UI stuck.
 */
async function runAction(button, label, fn) {
  if (state.busy) return;
  state.busy = true;

  const originalText = button ? button.textContent : null;
  if (button) {
    button.disabled = true;
    button.textContent = label;
  }

  try {
    await fn();
  } finally {
    state.busy = false;
    if (button) {
      button.disabled = false;
      button.textContent = originalText;
    }
  }
}

// ---------------------------------------------------------------------
// Screen 1: folder selection
// ---------------------------------------------------------------------

function validateSelectScreen() {
  const allFilled =
    state.mode === "dedupe"
      ? state.paths.a && state.paths.dest
      : state.paths.a && state.paths.b && state.paths.dest;
  document.getElementById("btn-continue-select").disabled = !allFilled;
}

Object.entries(pathInputs).forEach(([key, input]) => {
  input.addEventListener("input", () => {
    state.paths[key] = input.value.trim();
    validateSelectScreen();
  });
});

document.querySelectorAll('input[name="mode"]').forEach((radio) => {
  radio.addEventListener("change", (e) => {
    state.mode = e.target.value;
    const isDedupe = state.mode === "dedupe";
    document.getElementById("field-row-b").hidden = isDedupe;
    document.getElementById("dest-label").textContent = isDedupe ? "Quarantine folder" : "Destination";
    document.getElementById("dest-hint").textContent = isDedupe
      ? "(where duplicate copies will be moved)"
      : "(where exclusive files will be moved)";
    validateSelectScreen();
  });
});

const btnContinueSelect = document.getElementById("btn-continue-select");

btnContinueSelect.addEventListener("click", () => {
  runAction(btnContinueSelect, "Scanning folders…", async () => {
    const errorEl = document.getElementById("select-error");
    errorEl.hidden = true;
    try {
      const prescanBody =
        state.mode === "dedupe" ? { a: state.paths.a } : { a: state.paths.a, b: state.paths.b };
      const data = await api("/api/prescan", "POST", prescanBody);
      state.noisy = data.noisy;
      state.rules = {};
      updatePathSummary();
      showScreen("screen-prescan");
      loadVolumeInfo();

      const hasNoisy = state.noisy.length > 0;
      document.getElementById("prescan-controls").hidden = !hasNoisy;
      document.querySelector("#screen-prescan .table-wrap").hidden = !hasNoisy;
      document.getElementById("btn-start-scan").hidden = !hasNoisy;

      if (hasNoisy) {
        renderPrescanTable();
      } else {
        await startScan();
      }
    } catch (err) {
      errorEl.hidden = false;
      errorEl.textContent = err.message;
    }
  });
});

async function loadSettings() {
  try {
    const data = await api("/api/settings", "GET");
    if (data.last_paths) {
      state.paths = { ...data.last_paths };
      pathInputs.a.value = state.paths.a || "";
      pathInputs.b.value = state.paths.b || "";
      pathInputs.dest.value = state.paths.dest || "";
      validateSelectScreen();
    }
    state.ioWorkers = data.io_workers || 1;
    state.useCache = data.use_cache !== false;
    document.getElementById("input-io-workers").value = state.ioWorkers;
    document.getElementById("input-use-cache").checked = state.useCache;
  } catch (err) {
    // Settings are a convenience; failure must never block the app.
  }
}

async function loadCacheStats() {
  try {
    const data = await api("/api/cache/stats", "GET");
    document.getElementById("cache-settings-summary").textContent =
      `Cached hashes: ${data.row_count.toLocaleString()} files, ${formatBytes(data.db_size_bytes)}`;
  } catch (err) {
    document.getElementById("cache-settings-summary").textContent = "";
  }
}

document.getElementById("input-io-workers").addEventListener("input", (e) => {
  state.workersManuallySet = true;
  state.ioWorkers = Math.max(1, Math.min(32, Number(e.target.value) || 1));
});

document.getElementById("input-use-cache").addEventListener("change", (e) => {
  state.useCache = e.target.checked;
});

document.getElementById("btn-clear-cache").addEventListener("click", () => {
  const btn = document.getElementById("btn-clear-cache");
  runAction(btn, "Clearing…", async () => {
    await api("/api/cache/clear", "POST", {});
    await loadCacheStats();
  });
});

loadSettings();
loadCacheStats();

// ---------------------------------------------------------------------
// Folder browse modal
// ---------------------------------------------------------------------

document.querySelectorAll("[data-browse-target]").forEach((btn) => {
  btn.addEventListener("click", () => openBrowse(btn.dataset.browseTarget));
});

function openBrowse(target) {
  state.browseTarget = target;
  document.getElementById("browse-modal").hidden = false;
  browseTo(pathInputs[target].value.trim());
}

async function browseTo(path) {
  // Clicking through directories quickly must not stack up requests whose
  // responses could then arrive out of order and render the wrong folder.
  if (state.browseBusy) return;
  state.browseBusy = true;

  const errorEl = document.getElementById("browse-error");
  errorEl.hidden = true;
  try {
    const data = await api("/api/browse", "POST", { path });
    state.browseCurrentPath = data.path;
    document.getElementById("browse-current-path").textContent =
      data.path || "(choose a starting point)";
    renderBrowseList(data);
  } catch (err) {
    errorEl.hidden = false;
    errorEl.textContent = err.message;
  } finally {
    state.browseBusy = false;
  }
}

function renderBrowseList(data) {
  const list = document.getElementById("browse-list");
  list.innerHTML = "";

  if (data.parent !== null && data.parent !== undefined) {
    const up = document.createElement("li");
    up.textContent = ".. (parent folder)";
    up.addEventListener("click", () => browseTo(data.parent));
    list.appendChild(up);
  }

  for (const dir of data.dirs) {
    const li = document.createElement("li");
    li.textContent = dir.name;
    li.addEventListener("click", () => browseTo(dir.path));
    list.appendChild(li);
  }
}

document.getElementById("btn-browse-cancel").addEventListener("click", () => {
  document.getElementById("browse-modal").hidden = true;
});

document.getElementById("btn-browse-select").addEventListener("click", () => {
  if (!state.browseCurrentPath) return;
  const input = pathInputs[state.browseTarget];
  input.value = state.browseCurrentPath;
  state.paths[state.browseTarget] = state.browseCurrentPath;
  document.getElementById("browse-modal").hidden = true;
  validateSelectScreen();
});

// ---------------------------------------------------------------------
// Confirmation dialog (replaces window.confirm)
// ---------------------------------------------------------------------

/** Promise-based replacement for window.confirm, styled like the rest of the app. */
function confirmDialog({ title, message, confirmLabel = "Confirm" }) {
  return new Promise((resolve) => {
    const modal = document.getElementById("confirm-modal");
    const okBtn = document.getElementById("btn-confirm-ok");
    const cancelBtn = document.getElementById("btn-confirm-cancel");

    document.getElementById("confirm-title").textContent = title;
    document.getElementById("confirm-message").textContent = message;
    okBtn.textContent = confirmLabel;
    modal.hidden = false;
    okBtn.focus();

    function cleanup(result) {
      modal.hidden = true;
      okBtn.removeEventListener("click", onOk);
      cancelBtn.removeEventListener("click", onCancel);
      document.removeEventListener("keydown", onKey);
      resolve(result);
    }
    function onOk() {
      cleanup(true);
    }
    function onCancel() {
      cleanup(false);
    }
    function onKey(e) {
      if (e.key === "Escape") cleanup(false);
      if (e.key === "Enter") cleanup(true);
    }

    okBtn.addEventListener("click", onOk);
    cancelBtn.addEventListener("click", onCancel);
    document.addEventListener("keydown", onKey);
  });
}

// ---------------------------------------------------------------------
// Screen 2: noisy-directory pre-scan + scan progress
// ---------------------------------------------------------------------

async function loadVolumeInfo() {
  const container = document.getElementById("volume-info");
  container.innerHTML = `<p class="screen-hint">Detecting disk types…</p>`;
  try {
    // state.paths.b is stale in dedupe mode -- restored from saved settings on
    // load and never cleared when switching modes -- so it must not be sent
    // here: it would never actually be scanned, and combine()-ing it in would
    // skew suggested_workers off the volume that will actually be walked.
    const volumesBody =
      state.mode === "dedupe" ? { a: state.paths.a } : { a: state.paths.a, b: state.paths.b };
    const data = await api("/api/volumes", "POST", volumesBody);
    container.innerHTML = data.volumes
      .map(
        (v, i) =>
          `<p class="volume-line">Folder ${i === 0 ? "A" : "B"} — ${escapeHtml(v.path)} — ${escapeHtml(v.label)}</p>`
      )
      .join("");
    if (!state.workersManuallySet) {
      state.ioWorkers = data.suggested_workers;
      document.getElementById("input-io-workers").value = data.suggested_workers;
    }
  } catch (err) {
    container.innerHTML = `<p class="screen-hint">Could not detect disk types.</p>`;
  }
}

function renderPrescanTable() {
  const tbody = document.getElementById("prescan-tbody");

  tbody.innerHTML = state.noisy
    .map((dir, index) => {
      state.rules[dir.abs_path] = state.rules[dir.abs_path] || "skip";
      const trashOption =
        dir.root === "A"
          ? `<label><input type="radio" name="rule-${index}" value="trash" /> Trash</label>`
          : "";
      // Counts stop at a limit on huge directories, so mark them as a floor.
      const more = dir.counts_truncated ? "+" : "";
      return `
        <tr>
          <td class="path-cell">${escapeHtml(dir.rel_path)}</td>
          <td>${escapeHtml(dir.root)}</td>
          <td>${dir.file_count}${more}</td>
          <td>${escapeHtml(formatBytes(dir.total_bytes))}${more}</td>
          <td>
            <div class="radio-group">
              <label><input type="radio" name="rule-${index}" value="compare" /> Compare</label>
              <label><input type="radio" name="rule-${index}" value="skip" checked /> Skip</label>
              ${trashOption}
            </div>
          </td>
        </tr>
      `;
    })
    .join("");

  tbody.querySelectorAll("input[type=radio]").forEach((radio) => {
    radio.addEventListener("change", (e) => {
      const index = Number(e.target.name.split("-")[1]);
      state.rules[state.noisy[index].abs_path] = e.target.value;
    });
  });
}

document.getElementById("btn-set-all").addEventListener("click", () => {
  const value = document.getElementById("set-all-select").value;
  state.noisy.forEach((dir, index) => {
    const effective = value === "trash" && dir.root !== "A" ? "skip" : value;
    state.rules[dir.abs_path] = effective;
    const radio = document.querySelector(`input[name="rule-${index}"][value="${effective}"]`);
    if (radio) radio.checked = true;
  });
});

const btnStartScan = document.getElementById("btn-start-scan");

btnStartScan.addEventListener("click", () => {
  runAction(btnStartScan, "Scanning…", startScan);
});

async function startScan() {
  const errorEl = document.getElementById("prescan-error");
  errorEl.hidden = true;
  document.getElementById("scan-progress").hidden = false;
  document.getElementById("scan-progress-stall").hidden = true;

  await api("/api/settings", "POST", {
    last_paths: { ...state.paths },
    io_workers: state.ioWorkers,
    use_cache: state.useCache,
  }).catch(() => {});

  try {
    const data =
      state.mode === "dedupe"
        ? await api("/api/dedupe/scan", "POST", {
            folder: state.paths.a,
            rules: state.rules,
            io_workers: state.ioWorkers,
            use_cache: state.useCache,
          })
        : await api("/api/scan", "POST", {
            a: state.paths.a,
            b: state.paths.b,
            rules: state.rules,
            io_workers: state.ioWorkers,
            use_cache: state.useCache,
          });
    state.jobId = data.job_id;
    // Awaited so the caller stays "busy" for the whole scan, not just the POST.
    await pollProgress();
  } catch (err) {
    errorEl.hidden = false;
    errorEl.textContent = err.message;
    document.getElementById("scan-progress").hidden = true;
  }
}

function phaseLabel(phase, processed, total) {
  switch (phase) {
    case "scanning_a":
      return `Scanning folder A… (${processed.toLocaleString()} files found)`;
    case "scanning_b":
      return `Scanning folder B… (${processed.toLocaleString()} files found)`;
    case "comparing":
      return total > 0
        ? `Comparing files… (${processed.toLocaleString()}/${total.toLocaleString()} size groups)`
        : "Comparing files…";
    case "done":
      return "Done.";
    default:
      return "Working…";
  }
}

/**
 * Fill the progress stats grid.
 *
 * The byte figures are only meaningful once bucketing has produced a total,
 * which happens at the start of the comparing phase. During the two walk
 * phases the total is genuinely unknowable, so those cells show an em dash
 * rather than a misleading zero.
 */
function renderProgressStats(data) {
  const set = (id, value) => {
    document.getElementById(id).textContent = value;
  };
  const comparing = data.phase === "comparing" && data.bytes_to_resolve > 0;

  if (comparing) {
    const remaining = Math.max(0, data.bytes_to_resolve - data.bytes_resolved);
    const pct = Math.min(100, Math.round((data.bytes_resolved / data.bytes_to_resolve) * 100));
    set("stat-processed", `${formatBytes(data.bytes_resolved)} (${pct}%)`);
    set("stat-remaining", formatBytes(remaining));
    set("stat-total", formatBytes(data.bytes_to_resolve));
    set("stat-speed", data.throughput_bps ? formatThroughput(data.throughput_bps) : "measuring…");
    set(
      "stat-eta",
      data.eta_seconds !== null && data.eta_seconds !== undefined
        ? formatDuration(data.eta_seconds)
        : "estimating…"
    );
  } else {
    set("stat-processed", "—");
    set("stat-remaining", "—");
    set("stat-total", "—");
    set("stat-speed", "—");
    set("stat-eta", "—"); // never invent an estimate during the walk phases
  }

  set("stat-elapsed", formatElapsed(data.elapsed_seconds));

  const current = document.getElementById("scan-progress-current");
  current.textContent = data.current_path ? `Reading: ${data.current_path}` : "";
  current.title = data.current_path || "";
}

/** Elapsed time is a fact, not an estimate, so it is shown precisely. */
function formatElapsed(seconds) {
  const total = Math.floor(seconds || 0);
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  const pad = (n) => String(n).padStart(2, "0");
  return h > 0 ? `${h}:${pad(m)}:${pad(s)}` : `${m}:${pad(s)}`;
}

/** Poll until the scan finishes. Resolves once the report is loaded or it fails. */
function pollProgress() {
  const label = document.getElementById("scan-progress-label");
  const fill = document.getElementById("scan-progress-fill");
  const stallWarning = document.getElementById("scan-progress-stall");
  const errorEl = document.getElementById("prescan-error");

  return new Promise((resolve) => {
    // A single in-flight poll at a time: a slow response must not pile up
    // requests behind it.
    let polling = false;
    let lastSignature = null;
    let lastChangeAt = Date.now();

    const interval = setInterval(async () => {
      if (polling) return;
      polling = true;

      try {
        const data = await api(`/api/progress?job=${encodeURIComponent(state.jobId)}`, "GET");
        label.textContent = phaseLabel(data.phase, data.processed, data.total);
        renderProgressStats(data);

        // During comparing, drive the bar by bytes -- it matches the numbers
        // in the stats grid, and a bucket count would disagree with them.
        if (data.phase === "comparing" && data.bytes_to_resolve > 0) {
          fill.classList.remove("indeterminate");
          fill.style.width = `${Math.min(
            100,
            Math.round((data.bytes_resolved / data.bytes_to_resolve) * 100)
          )}%`;
        } else if (data.total > 0) {
          fill.classList.remove("indeterminate");
          fill.style.width = `${Math.min(100, Math.round((data.processed / data.total) * 100))}%`;
        } else {
          fill.classList.add("indeterminate");
        }

        // A signature of "is anything moving" -- distinguishes slow from
        // stuck without guessing at a specific number the server didn't send.
        const signature = `${data.processed}|${data.bytes_resolved}|${data.current_path}`;
        if (signature !== lastSignature) {
          lastSignature = signature;
          lastChangeAt = Date.now();
          stallWarning.hidden = true;
        } else if (Date.now() - lastChangeAt > 30000) {
          stallWarning.hidden = false;
        }

        if (data.status === "done") {
          clearInterval(interval);
          state.cacheHits = data.cache_hits;
          state.cacheMisses = data.cache_misses;
          if (state.mode === "dedupe") {
            await loadDedupeReport();
          } else {
            await loadReport();
          }
          resolve();
        } else if (data.status === "error") {
          clearInterval(interval);
          errorEl.hidden = false;
          errorEl.textContent = data.error || "scan failed";
          resolve();
        }
      } catch (err) {
        clearInterval(interval);
        errorEl.hidden = false;
        errorEl.textContent = err.message;
        resolve();
      } finally {
        polling = false;
      }
    }, 400);
  });
}

// ---------------------------------------------------------------------
// Screen 3: report
// ---------------------------------------------------------------------

async function loadReport() {
  const data = await api(`/api/report?job=${encodeURIComponent(state.jobId)}`, "GET");
  state.rows = data.rows;
  state.errors = data.errors;
  state.stats = data.stats;
  state.selected = new Set(state.rows.filter((r) => r.status === "exclusive").map((r) => r.id));

  const cacheSummary = document.getElementById("cache-hit-summary");
  if (state.useCache && (state.cacheHits || state.cacheMisses)) {
    cacheSummary.hidden = false;
    cacheSummary.textContent = `${state.cacheHits} files reused from cache, ${state.cacheMisses} newly hashed.`;
  } else {
    cacheSummary.hidden = true;
  }

  renderReportTable();
  renderReportSummary();
  renderErrorsPanel();
  showScreen("screen-report");
}

function renderReportSummary() {
  const presentInB = state.rows.filter((r) => r.status === "present_in_b").length;
  const shown = state.filteredRows.length;
  document.getElementById("report-summary").textContent =
    `${state.rows.length.toLocaleString()} files scanned in folder A. ` +
    `${presentInB.toLocaleString()} already exist in folder B (hidden from this table). ` +
    `Showing ${shown.toLocaleString()}.`;
}

function renderErrorsPanel() {
  const panel = document.getElementById("errors-panel");
  const list = document.getElementById("errors-list");
  const summary = document.getElementById("errors-summary");

  if (state.errors.length === 0) {
    panel.hidden = true;
    return;
  }

  panel.hidden = false;
  summary.textContent = `Read errors (${state.errors.length})`;
  list.innerHTML = state.errors
    .map((e) => `<li>${escapeHtml(e.path)} — ${escapeHtml(e.error)}</li>`)
    .join("");
}

function getFilteredSortedRows() {
  const text = state.filterText.toLowerCase();
  let rows = state.rows.filter((r) => r.status !== "present_in_b");
  rows = rows.filter((r) => state.statusFilters.has(r.status));
  if (text) {
    rows = rows.filter((r) => r.rel_path.toLowerCase().includes(text));
  }

  const dir = state.sortDir === "asc" ? 1 : -1;
  rows = rows.slice().sort((a, b) => {
    const va = a[state.sortKey];
    const vb = b[state.sortKey];
    if (va < vb) return -1 * dir;
    if (va > vb) return 1 * dir;
    return 0;
  });

  return rows;
}

function rowHtml(row) {
  const checked = state.selected.has(row.id) ? "checked" : "";
  const dupNote =
    row.status === "internal_copy" && row.duplicate_of
      ? `<span class="duplicate-note">copy of ${escapeHtml(row.duplicate_of)}</span>`
      : "";
  return `
    <tr>
      <td><input type="checkbox" data-row-id="${escapeHtml(row.id)}" ${checked} /></td>
      <td class="path-cell">${escapeHtml(row.rel_path)}${dupNote}</td>
      <td>${escapeHtml(formatBytes(row.size))}</td>
      <td><span class="status-badge status-${escapeHtml(row.status)}">${escapeHtml(
        statusLabel(row.status)
      )}</span></td>
    </tr>
  `;
}

// Must match the `height` declared for #report-table td in style.css.
const ROW_HEIGHT = 36;
const BUFFER_ROWS = 12; // rendered above and below the viewport, to absorb fast scrolling

function renderReportTable() {
  state.filteredRows = getFilteredSortedRows();
  const wrap = document.getElementById("report-table-wrap");
  wrap.scrollTop = 0;
  renderVisibleRows();
  updateSelectionSummary();
}

/**
 * Render only the rows currently in view, padded above and below by spacer
 * rows that reproduce the full scroll height.
 *
 * Without this, a report of 100k rows builds ~400k DOM nodes in one
 * synchronous pass and freezes the tab.
 */
function renderVisibleRows() {
  const wrap = document.getElementById("report-table-wrap");
  const tbody = document.getElementById("report-tbody");
  const rows = state.filteredRows;

  const viewportHeight = wrap.clientHeight || 600;
  const first = Math.max(0, Math.floor(wrap.scrollTop / ROW_HEIGHT) - BUFFER_ROWS);
  const visibleCount = Math.ceil(viewportHeight / ROW_HEIGHT) + BUFFER_ROWS * 2;
  const last = Math.min(rows.length, first + visibleCount);

  const topPad = first * ROW_HEIGHT;
  const bottomPad = Math.max(0, (rows.length - last) * ROW_HEIGHT);

  const html = [];
  if (topPad > 0) {
    html.push(`<tr class="spacer"><td colspan="4" style="height:${topPad}px"></td></tr>`);
  }
  for (let i = first; i < last; i += 1) {
    html.push(rowHtml(rows[i]));
  }
  if (bottomPad > 0) {
    html.push(`<tr class="spacer"><td colspan="4" style="height:${bottomPad}px"></td></tr>`);
  }
  tbody.innerHTML = html.join("");
}

// One delegated listener on the tbody, registered once. Rows are recreated on
// every scroll, so per-row listeners would leak and be rebound constantly.
document.getElementById("report-tbody").addEventListener("change", (e) => {
  const cb = e.target.closest("input[type=checkbox][data-row-id]");
  if (!cb) return;
  const id = cb.dataset.rowId;
  if (cb.checked) state.selected.add(id);
  else state.selected.delete(id);
  updateSelectionSummary();
});

// Re-render on scroll, throttled to one frame so a fast flick doesn't queue
// dozens of renders.
document.getElementById("report-table-wrap").addEventListener("scroll", () => {
  if (state.scrollPending) return;
  state.scrollPending = true;
  requestAnimationFrame(() => {
    state.scrollPending = false;
    renderVisibleRows();
  });
});

function updateSelectionSummary() {
  const selectedRows = state.rows.filter((r) => state.selected.has(r.id));
  const totalBytes = selectedRows.reduce((sum, r) => sum + r.size, 0);
  document.getElementById(
    "selection-summary"
  ).textContent = `${selectedRows.length} files selected, ${formatBytes(totalBytes)}`;
}

document.querySelectorAll("#report-table th.sortable").forEach((th) => {
  th.addEventListener("click", () => {
    const key = th.dataset.sortKey;
    if (state.sortKey === key) {
      state.sortDir = state.sortDir === "asc" ? "desc" : "asc";
    } else {
      state.sortKey = key;
      state.sortDir = "asc";
    }
    renderReportTable();
  });
});

document.querySelectorAll("[data-status-filter]").forEach((cb) => {
  cb.addEventListener("change", () => {
    const status = cb.dataset.statusFilter;
    if (cb.checked) state.statusFilters.add(status);
    else state.statusFilters.delete(status);
    renderReportTable();
  });
});

let filterTimer = null;
document.getElementById("report-filter").addEventListener("input", (e) => {
  state.filterText = e.target.value;
  clearTimeout(filterTimer);
  filterTimer = setTimeout(renderReportTable, 150);
});

document.getElementById("btn-select-all").addEventListener("click", () => {
  state.filteredRows.forEach((r) => state.selected.add(r.id));
  renderVisibleRows();
  updateSelectionSummary();
});

document.getElementById("btn-select-none").addEventListener("click", () => {
  state.filteredRows.forEach((r) => state.selected.delete(r.id));
  renderVisibleRows();
  updateSelectionSummary();
});

const btnApply = document.getElementById("btn-apply");

btnApply.addEventListener("click", async () => {
  const errorEl = document.getElementById("report-error");
  errorEl.hidden = true;

  const selectedRows = state.rows.filter((r) => state.selected.has(r.id));
  if (selectedRows.length === 0) {
    errorEl.hidden = false;
    errorEl.textContent = "Select at least one file to move.";
    return;
  }

  const totalBytes = selectedRows.reduce((sum, r) => sum + r.size, 0);
  const confirmed = await confirmDialog({
    title: "Move files?",
    message:
      `${selectedRows.length.toLocaleString()} file(s), ${formatBytes(totalBytes)} will be moved to:\n` +
      `${state.paths.dest}\n\nThis cannot be undone from this UI.`,
    confirmLabel: "Move files",
  });
  if (!confirmed) return;

  runAction(btnApply, "Moving files…", async () => {
    try {
      const result = await api("/api/apply", "POST", {
        job_id: state.jobId,
        dest: state.paths.dest,
        selected: selectedRows.map((r) => r.id),
      });
      renderResult(result);
      showScreen("screen-result");
    } catch (err) {
      errorEl.hidden = false;
      errorEl.textContent = `Failed to move files: ${err.message}`;
    }
  });
});

// ---------------------------------------------------------------------
// Screen 4: result
// ---------------------------------------------------------------------

function renderResult(result) {
  const list = document.getElementById("result-summary");
  list.innerHTML = "";

  const items = [
    ["Moved", result.moved.length],
    ["Skipped (already identical at destination)", result.skipped_identical.length],
    ["Renamed on collision", result.renamed.length],
    ["Moved to trash", result.trashed.length],
    ["Errors", result.errors.length],
  ];

  for (const [label, count] of items) {
    const li = document.createElement("li");
    li.textContent = `${label}: ${count}`;
    list.appendChild(li);
  }

  document.getElementById(
    "result-report-path"
  ).textContent = `Audit report written to: ${result.report_path}`;
}

document.getElementById("btn-start-over").addEventListener("click", () => {
  resetState();
  showScreen("screen-select");
});

function resetState() {
  state.paths = { a: "", b: "", dest: "" };
  state.noisy = [];
  state.rules = {};
  state.jobId = null;
  state.rows = [];
  state.errors = [];
  state.stats = null;
  state.selected = new Set();
  state.filterText = "";
  state.sortKey = "rel_path";
  state.sortDir = "asc";
  state.statusFilters = new Set(["exclusive", "internal_copy", "unreadable"]);
  state.filteredRows = [];

  Object.values(pathInputs).forEach((input) => {
    input.value = "";
  });
  document.getElementById("report-filter").value = "";
  document.querySelectorAll("[data-status-filter]").forEach((cb) => {
    cb.checked = true;
  });
  document.getElementById("btn-continue-select").disabled = true;
  document.getElementById("btn-start-scan").disabled = false;
  document.getElementById("scan-progress").hidden = true;
  document.getElementById("report-error").hidden = true;
  updatePathSummary();

  state.mode = "compare";
  state.dedupe = { groups: [], emptyGroup: null, keepRules: [], kept: {}, overrides: {} };

  document.querySelector('input[name="mode"][value="compare"]').checked = true;
  document.getElementById("field-row-b").hidden = false;
  document.getElementById("dest-label").textContent = "Destination";
  document.getElementById("dest-hint").textContent = "(where exclusive files will be moved)";
}

// ---------------------------------------------------------------------
// Screen 3b: dedupe report (grouped duplicates)
// ---------------------------------------------------------------------

async function loadDedupeReport() {
  const errorEl = document.getElementById("dedupe-report-error");
  errorEl.hidden = true;
  try {
    const data = await api(`/api/dedupe/report?job=${encodeURIComponent(state.jobId)}`, "GET");
    state.dedupe.groups = data.groups;
    state.dedupe.emptyGroup = data.empty_group;
    state.dedupe.keepRules = [];
    state.dedupe.overrides = {};
    await refreshKeptSelection();
    renderKeepRules();
    renderDedupeGroups();
    renderDedupeSummary(data);
    showScreen("screen-dedupe-report");
  } catch (err) {
    errorEl.hidden = false;
    errorEl.textContent = err.message;
  }
}

/** Re-resolve kept-copy preselection from the server. Called on every rule change. */
async function refreshKeptSelection() {
  const data = await api("/api/dedupe/resolve", "POST", {
    job_id: state.jobId,
    keep_rules: state.dedupe.keepRules,
  });
  state.dedupe.kept = data.kept;
}

function renderDedupeSummary(stats) {
  const totalWasted = state.dedupe.groups.reduce((sum, g) => sum + g.wasted_bytes, 0);
  document.getElementById("dedupe-report-summary").textContent =
    `${state.dedupe.groups.length} duplicate group(s) found, ${formatBytes(totalWasted)} reclaimable.`;
}

function renderKeepRules() {
  const list = document.getElementById("keep-rules-list");
  const emptyNote = document.getElementById("keep-rules-empty");
  list.innerHTML = "";
  emptyNote.hidden = state.dedupe.keepRules.length > 0;

  state.dedupe.keepRules.forEach((rule, index) => {
    const li = document.createElement("li");

    const pathSpan = document.createElement("span");
    pathSpan.className = "rule-path";
    pathSpan.textContent = rule;
    li.appendChild(pathSpan);

    const upBtn = document.createElement("button");
    upBtn.type = "button";
    upBtn.className = "btn btn-secondary";
    upBtn.textContent = "↑";
    upBtn.disabled = index === 0;
    upBtn.addEventListener("click", () => moveKeepRule(index, index - 1));
    li.appendChild(upBtn);

    const downBtn = document.createElement("button");
    downBtn.type = "button";
    downBtn.className = "btn btn-secondary";
    downBtn.textContent = "↓";
    downBtn.disabled = index === state.dedupe.keepRules.length - 1;
    downBtn.addEventListener("click", () => moveKeepRule(index, index + 1));
    li.appendChild(downBtn);

    const removeBtn = document.createElement("button");
    removeBtn.type = "button";
    removeBtn.className = "btn btn-secondary";
    removeBtn.textContent = "Remove";
    removeBtn.addEventListener("click", () => removeKeepRule(index));
    li.appendChild(removeBtn);

    list.appendChild(li);
  });
}

async function addKeepRule(rulePath) {
  if (state.dedupe.keepRules.includes(rulePath)) return;
  state.dedupe.keepRules.unshift(rulePath); // new rules default to top priority
  state.dedupe.overrides = {}; // a rule change supersedes manual per-group overrides
  const errorEl = document.getElementById("dedupe-report-error");
  try {
    await refreshKeptSelection();
    errorEl.hidden = true;
  } catch (err) {
    errorEl.hidden = false;
    errorEl.textContent = err.message;
  }
  renderKeepRules();
  renderDedupeGroups();
}

async function moveKeepRule(from, to) {
  const rules = state.dedupe.keepRules;
  const [moved] = rules.splice(from, 1);
  rules.splice(to, 0, moved);
  const errorEl = document.getElementById("dedupe-report-error");
  try {
    await refreshKeptSelection();
    errorEl.hidden = true;
  } catch (err) {
    errorEl.hidden = false;
    errorEl.textContent = err.message;
  }
  renderKeepRules();
  renderDedupeGroups();
}

async function removeKeepRule(index) {
  state.dedupe.keepRules.splice(index, 1);
  const errorEl = document.getElementById("dedupe-report-error");
  try {
    await refreshKeptSelection();
    errorEl.hidden = true;
  } catch (err) {
    errorEl.hidden = false;
    errorEl.textContent = err.message;
  }
  renderKeepRules();
  renderDedupeGroups();
}

/** The rel_path currently kept for a group: a manual override, else the server's resolution. */
function keptPathFor(digest) {
  return state.dedupe.overrides[digest] ?? state.dedupe.kept[digest];
}

function renderDedupeGroups() {
  const container = document.getElementById("dedupe-groups");
  container.innerHTML = "";

  for (const group of state.dedupe.groups) {
    const groupEl = document.createElement("div");
    groupEl.className = "duplicate-group";

    const header = document.createElement("div");
    header.className = "duplicate-group-header";
    header.innerHTML = `<span>${group.members.length} copies, ${formatBytes(group.size)} each</span><span>${formatBytes(group.wasted_bytes)} reclaimable</span>`;
    groupEl.appendChild(header);

    const keptPath = keptPathFor(group.digest);

    for (const member of group.members) {
      const row = document.createElement("div");
      row.className = "duplicate-group-member";

      const radio = document.createElement("input");
      radio.type = "radio";
      radio.name = `keep-${group.digest}`;
      radio.checked = member.rel_path === keptPath;
      radio.addEventListener("change", () => {
        state.dedupe.overrides[group.digest] = member.rel_path;
        renderDedupeGroups();
        renderSelectionSummary();
      });
      row.appendChild(radio);

      const pathSpan = document.createElement("span");
      pathSpan.className = "member-path";
      pathSpan.textContent = member.rel_path;
      row.appendChild(pathSpan);

      const keepFolderBtn = document.createElement("button");
      keepFolderBtn.type = "button";
      keepFolderBtn.className = "btn btn-secondary";
      keepFolderBtn.textContent = "Keep everything in this folder";
      const parentDir = member.rel_path.includes("/")
        ? member.rel_path.slice(0, member.rel_path.lastIndexOf("/"))
        : "";
      keepFolderBtn.disabled = parentDir === "";
      keepFolderBtn.addEventListener("click", () => addKeepRule(parentDir));
      row.appendChild(keepFolderBtn);

      groupEl.appendChild(row);
    }

    container.appendChild(groupEl);
  }

  renderDedupeEmptyGroup();
  renderSelectionSummary();
}

function renderDedupeEmptyGroup() {
  const panel = document.getElementById("dedupe-empty-group-panel");
  const list = document.getElementById("dedupe-empty-group-list");
  const group = state.dedupe.emptyGroup;

  panel.hidden = group === null;
  if (group === null) return;

  list.innerHTML = "";
  for (const member of group.members) {
    const li = document.createElement("li");
    li.textContent = member.rel_path;
    list.appendChild(li);
  }
}

/**
 * Every member not currently kept, across all real groups (the empty group is
 * excluded — D5), plus their total byte count.
 *
 * Computed in a single O(N) pass over state.dedupe.groups -- member.size is
 * already at hand while iterating, so there is no need for a second pass to
 * look sizes back up afterward (that second pass used to be a per-selected-id
 * linear .find() across every group's every member, i.e. quadratic).
 */
function computeDedupeSelection() {
  const ids = [];
  let bytes = 0;
  for (const group of state.dedupe.groups) {
    const keptPath = keptPathFor(group.digest);
    for (const member of group.members) {
      if (member.rel_path !== keptPath) {
        ids.push(member.id);
        bytes += member.size;
      }
    }
  }
  return { ids, bytes };
}

function renderSelectionSummary() {
  const { ids, bytes } = computeDedupeSelection();
  document.getElementById("dedupe-selection-summary").textContent =
    `${ids.length} file(s) to quarantine, ${formatBytes(bytes)}`;
}

document.getElementById("btn-dedupe-apply").addEventListener("click", () => {
  const btn = document.getElementById("btn-dedupe-apply");
  runAction(btn, "Moving…", async () => {
    const errorEl = document.getElementById("dedupe-report-error");
    errorEl.hidden = true;

    const { ids: selected } = computeDedupeSelection();
    if (selected.length === 0) {
      errorEl.hidden = false;
      errorEl.textContent = "Select at least one file to move.";
      return;
    }

    const confirmed = await confirmDialog({
      title: "Move duplicates to quarantine",
      message: `Move ${selected.length} file(s) to ${state.paths.dest}? Nothing is deleted -- ` +
        `you can review the quarantine folder afterward.`,
      confirmLabel: "Move files",
    });
    if (!confirmed) return;

    try {
      const data = await api("/api/dedupe/apply", "POST", {
        job_id: state.jobId,
        dest: state.paths.dest,
        selected,
        keep_rules: state.dedupe.keepRules,
      });
      renderDedupeResult(data);
      showScreen("screen-result");
    } catch (err) {
      errorEl.hidden = false;
      errorEl.textContent = err.message;
    }
  });
});

function renderDedupeResult(data) {
  const list = document.getElementById("result-summary");
  list.innerHTML = "";
  const items = [
    `${data.moved.length} file(s) moved to quarantine`,
    `${data.skipped_identical.length} skipped (identical file already at destination)`,
    `${data.renamed.length} renamed to avoid a collision`,
    `${data.trashed.length} noisy director(y/ies) moved to trash`,
    `${data.errors.length} error(s)`,
  ];
  for (const text of items) {
    const li = document.createElement("li");
    li.textContent = text;
    list.appendChild(li);
  }
  document.getElementById("result-report-path").textContent = `Audit report: ${data.report_path}`;
}
