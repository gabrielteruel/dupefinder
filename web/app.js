"use strict";

// ---------------------------------------------------------------------
// State
// ---------------------------------------------------------------------

const state = {
  paths: { a: "", b: "", dest: "" },
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
  busy: false, // a request-driven action is running
  browseBusy: false, // a folder-browse request is running
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
  const allFilled = state.paths.a && state.paths.b && state.paths.dest;
  document.getElementById("btn-continue-select").disabled = !allFilled;
}

Object.entries(pathInputs).forEach(([key, input]) => {
  input.addEventListener("input", () => {
    state.paths[key] = input.value.trim();
    validateSelectScreen();
  });
});

const btnContinueSelect = document.getElementById("btn-continue-select");

btnContinueSelect.addEventListener("click", () => {
  runAction(btnContinueSelect, "Scanning folders…", async () => {
    const errorEl = document.getElementById("select-error");
    errorEl.hidden = true;
    try {
      const data = await api("/api/prescan", "POST", { a: state.paths.a, b: state.paths.b });
      state.noisy = data.noisy;
      state.rules = {};
      showScreen("screen-prescan");

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
// Screen 2: noisy-directory pre-scan + scan progress
// ---------------------------------------------------------------------

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

  try {
    const data = await api("/api/scan", "POST", {
      a: state.paths.a,
      b: state.paths.b,
      rules: state.rules,
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
      return "Scanning folder A…";
    case "scanning_b":
      return "Scanning folder B…";
    case "comparing":
      return total > 0 ? `Comparing files… (${processed}/${total})` : "Comparing files…";
    case "done":
      return "Done.";
    default:
      return "Working…";
  }
}

/** Poll until the scan finishes. Resolves once the report is loaded or it fails. */
function pollProgress() {
  const label = document.getElementById("scan-progress-label");
  const fill = document.getElementById("scan-progress-fill");
  const errorEl = document.getElementById("prescan-error");

  return new Promise((resolve) => {
    // A single in-flight poll at a time: a slow response must not pile up
    // requests behind it.
    let polling = false;

    const interval = setInterval(async () => {
      if (polling) return;
      polling = true;

      try {
        const data = await api(`/api/progress?job=${encodeURIComponent(state.jobId)}`, "GET");
        label.textContent = phaseLabel(data.phase, data.processed, data.total);

        if (data.total > 0) {
          fill.classList.remove("indeterminate");
          fill.style.width = `${Math.min(100, Math.round((data.processed / data.total) * 100))}%`;
        } else {
          fill.classList.add("indeterminate");
        }

        if (data.status === "done") {
          clearInterval(interval);
          await loadReport();
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

  renderReportSummary();
  renderErrorsPanel();
  renderReportTable();
  showScreen("screen-report");
}

function renderReportSummary() {
  const presentInB = state.rows.filter((r) => r.status === "present_in_b").length;
  document.getElementById("report-summary").textContent =
    `${state.rows.length} files scanned in folder A. ` +
    `${presentInB} already exist in folder B (hidden from this table).`;
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

function renderReportTable() {
  const tbody = document.getElementById("report-tbody");
  const filtered = getFilteredSortedRows();
  tbody.innerHTML = filtered.map(rowHtml).join("");

  tbody.querySelectorAll("input[type=checkbox][data-row-id]").forEach((cb) => {
    cb.addEventListener("change", () => {
      const id = cb.dataset.rowId;
      if (cb.checked) state.selected.add(id);
      else state.selected.delete(id);
      updateSelectionSummary();
    });
  });

  updateSelectionSummary();
}

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

document.getElementById("report-filter").addEventListener("input", (e) => {
  state.filterText = e.target.value;
  renderReportTable();
});

document.getElementById("btn-select-all").addEventListener("click", () => {
  getFilteredSortedRows().forEach((r) => state.selected.add(r.id));
  renderReportTable();
});

document.getElementById("btn-select-none").addEventListener("click", () => {
  getFilteredSortedRows().forEach((r) => state.selected.delete(r.id));
  renderReportTable();
});

const btnApply = document.getElementById("btn-apply");

btnApply.addEventListener("click", () => {
  const selectedRows = state.rows.filter((r) => state.selected.has(r.id));
  if (selectedRows.length === 0) {
    alert("Select at least one file to move.");
    return;
  }

  const totalBytes = selectedRows.reduce((sum, r) => sum + r.size, 0);
  const confirmed = confirm(
    `Move ${selectedRows.length} file(s) (${formatBytes(totalBytes)}) to:\n${state.paths.dest}\n\n` +
      "This cannot be undone from this UI. Continue?"
  );
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
      alert(`Failed to move files: ${err.message}`);
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
}
