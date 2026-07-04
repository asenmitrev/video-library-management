"use strict";

const $ = (id) => document.getElementById(id);

const state = {
  indexing: false,
  hasFolders: false,
  totalSegments: 0,
  lastQuery: null,
  pollTimer: null,
};

// ---------- helpers ----------

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch {}
    throw new Error(detail);
  }
  return res.json();
}

function fmtTime(sec) {
  sec = Math.max(0, Math.floor(sec));
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = sec % 60;
  const mm = String(m).padStart(h ? 2 : 1, "0");
  const ss = String(s).padStart(2, "0");
  return h ? `${h}:${mm}:${ss}` : `${mm}:${ss}`;
}

function fmtEta(sec) {
  if (sec < 60) return "under a minute left";
  if (sec < 3600) return `about ${Math.round(sec / 60)} min left`;
  return `about ${(sec / 3600).toFixed(1)} h left`;
}

function fmtBytes(n) {
  if (n < 1024 ** 2) return `${(n / 1024).toFixed(0)} KB`;
  if (n < 1024 ** 3) return `${(n / 1024 ** 2).toFixed(0)} MB`;
  return `${(n / 1024 ** 3).toFixed(2)} GB`;
}

// Relative relevance: dots scaled against the best hit in this result set.
function relevanceDots(score, topScore) {
  const ratio = topScore > 0 ? score / topScore : 0;
  const filled = Math.max(1, Math.round(ratio * 5));
  return "●".repeat(filled) + "○".repeat(5 - filled);
}

function showEmpty(title, hint) {
  $("empty-title").textContent = title;
  $("empty-hint").textContent = hint || "";
  $("empty-state").classList.remove("hidden");
}
function hideEmpty() { $("empty-state").classList.add("hidden"); }

// ---------- search ----------

async function runSearch(query) {
  state.lastQuery = query;
  $("status-line").textContent = "Searching…";
  try {
    const data = await api(`/api/search?q=${encodeURIComponent(query)}&k=12`);
    renderResults(data);
  } catch (err) {
    $("status-line").textContent = `Search failed: ${err.message}`;
  }
}

function renderResults(data) {
  const grid = $("results");
  grid.innerHTML = "";
  hideEmpty();

  if (!data.results.length) {
    $("status-line").textContent = "";
    if (!state.hasFolders) {
      showEmpty("No folders in your library yet",
        "Click “+ Add folder” to index a folder of videos first.");
    } else if (state.indexing && !state.totalSegments) {
      showEmpty("Still indexing…",
        "Results will appear as soon as the first videos are indexed. You can search again any time.");
    } else {
      showEmpty(`No results for “${data.query}”`,
        state.indexing
          ? "Indexing is still running — more videos become searchable as it progresses."
          : "Try describing the scene differently, e.g. colors, objects, or setting.");
    }
    return;
  }

  const top = data.results[0].score;
  $("status-line").textContent = state.indexing
    ? `${data.results.length} results (indexing still running — more videos become searchable as it progresses)`
    : `${data.results.length} results`;

  for (const r of data.results) {
    const card = document.createElement("div");
    card.className = "result-card" + (r.exists ? "" : " missing");
    card.title = r.exists
      ? `${r.path}\nClick to open in your video player`
      : `${r.path}\n(file is missing or its drive is disconnected)`;

    const thumb = r.thumb_url
      ? `<img src="${r.thumb_url}" alt="" loading="lazy">`
      : `<div class="no-thumb">🎞</div>`;

    card.innerHTML = `
      <div class="thumb-wrap">${thumb}
        <span class="timestamp">${fmtTime(r.start_sec)}</span>
      </div>
      <div class="card-body">
        <div class="card-title"></div>
        <div class="card-meta">
          <span class="relevance" title="Relevance (relative to best match)">${relevanceDots(r.score, top)}</span>
          <button class="reveal-link">Show in Finder</button>
        </div>
      </div>`;
    card.querySelector(".card-title").textContent = r.filename;

    if (r.exists) {
      card.addEventListener("click", () => {
        api("/api/open", {
          method: "POST",
          body: JSON.stringify({ path: r.path, start_sec: r.start_sec }),
        }).catch((e) => { $("status-line").textContent = e.message; });
      });
    }
    card.querySelector(".reveal-link").addEventListener("click", (ev) => {
      ev.stopPropagation();
      api("/api/reveal", { method: "POST", body: JSON.stringify({ path: r.path }) })
        .catch((e) => { $("status-line").textContent = e.message; });
    });

    grid.appendChild(card);
  }
}

// ---------- model install (blocking) ----------

function showInstallingDialog() {
  const dlg = $("installing-dialog");
  if (!dlg.open) dlg.showModal();
}
function hideInstallingDialog() {
  const dlg = $("installing-dialog");
  if (dlg.open) dlg.close();
}
// Model download must run to completion; don't let Esc dismiss the modal
// and make the rest of the UI look usable while it's still loading.
$("installing-dialog").addEventListener("cancel", (ev) => ev.preventDefault());

// ---------- indexing progress ----------

async function pollStatus() {
  let st;
  try { st = await api("/api/index/status"); } catch { return; }

  const busy = ["indexing", "loading_model"].includes(st.state)
    || st.queued_folders.length > 0;
  const banner = $("progress-banner");
  state.indexing = busy;

  if (st.state === "error") {
    hideInstallingDialog();
    banner.classList.remove("hidden");
    banner.classList.add("error");
    $("progress-label").textContent = `Indexing failed: ${st.error}`;
    $("progress-detail").textContent = "";
    $("progress-fill").style.width = "0";
    return;
  }
  banner.classList.remove("error");

  if (!busy) {
    hideInstallingDialog();
    if (!banner.classList.contains("hidden") && st.state === "done") {
      const failed = st.failed_files ? `, ${st.failed_files} failed` : "";
      $("status-line").textContent =
        `Indexing finished: ${st.done_files} new, ${st.skipped_files} unchanged${failed}.`;
      refreshLibraryInfo();
    }
    banner.classList.add("hidden");
    return;
  }

  banner.classList.remove("hidden");
  const fill = $("progress-fill");
  const installingFill = $("installing-fill");

  if (st.state === "loading_model") {
    const downloaded = st.download_bytes || 0;
    const total = st.download_total_bytes;
    let detail = "First run downloads ~1.4 GB once; afterwards this takes seconds.";
    if (total) {
      const pct = Math.min(100, (downloaded / total) * 100);
      fill.classList.remove("indeterminate");
      installingFill.classList.remove("indeterminate");
      fill.style.width = installingFill.style.width = `${pct}%`;
      detail = `${fmtBytes(downloaded)} / ${fmtBytes(total)} (${pct.toFixed(0)}%)`;
    } else {
      fill.classList.add("indeterminate");
      installingFill.classList.add("indeterminate");
      if (downloaded) detail = `${fmtBytes(downloaded)} downloaded…`;
    }
    $("progress-label").textContent = "Preparing the vision model…";
    $("progress-detail").textContent = detail;
    $("installing-detail").textContent = detail;
    showInstallingDialog();
    return;
  }
  hideInstallingDialog();

  fill.classList.remove("indeterminate");
  const processed = st.done_files + st.skipped_files + st.failed_files;
  const pct = st.total_files ? (processed / st.total_files) * 100 : 0;
  fill.style.width = `${pct}%`;
  $("progress-label").textContent =
    `Indexing ${processed}/${st.total_files} files` +
    (st.eta_sec != null ? ` — ${fmtEta(st.eta_sec)}` : "");
  $("progress-detail").textContent = st.current_file
    ? st.current_file.split("/").pop() : "";
}

function startPolling() {
  if (!state.pollTimer) {
    pollStatus();
    state.pollTimer = setInterval(pollStatus, 1000);
  }
}

// ---------- library / folders ----------

async function refreshLibraryInfo() {
  try {
    const data = await api("/api/folders");
    state.hasFolders = data.folders.length > 0;
    state.totalSegments = data.stats.segments;
    if (!state.lastQuery) {
      if (!state.hasFolders) {
        showEmpty("Welcome! Your library is empty.",
          "Click “+ Add folder” and pick a folder of videos. Indexing runs in the background — you can search as soon as the first files are done.");
      } else {
        hideEmpty();
        $("status-line").textContent =
          `${data.stats.files} videos indexed (${data.stats.segments} searchable moments). Type a description above to search.`;
      }
    }
  } catch { /* server briefly unavailable */ }
}

async function addFolder() {
  const btn = $("add-folder-btn");
  btn.disabled = true;
  try {
    const { folder } = await api("/api/select-folder", { method: "POST" });
    if (folder) {
      await api("/api/index", { method: "POST", body: JSON.stringify({ folder }) });
      state.hasFolders = true;
      hideEmpty();
    }
  } catch (err) {
    $("status-line").textContent = `Could not add folder: ${err.message}`;
  } finally {
    btn.disabled = false;
  }
}

// ---------- settings ----------

async function openSettings() {
  try {
    const [s, f] = await Promise.all([api("/api/settings"), api("/api/folders")]);
    $("set-datadir").textContent = s.data_dir;
    $("set-stats").textContent = `${s.files} videos, ${s.segments} moments`;
    $("set-model").textContent = `${s.model} (${s.device})`;
    $("set-folders").textContent = f.folders.join("\n") || "none";
  } catch { /* show dialog anyway */ }
  $("settings-dialog").showModal();
}

async function resetLibrary() {
  if (!confirm("Delete the entire search index and thumbnails? Your video files are not touched.")) return;
  try {
    await api("/api/reset", { method: "POST" });
    $("settings-dialog").close();
    state.lastQuery = null;
    $("results").innerHTML = "";
    refreshLibraryInfo();
  } catch (err) {
    alert(`Could not clear: ${err.message}`);
  }
}

// ---------- wiring ----------

$("search-form").addEventListener("submit", (ev) => {
  ev.preventDefault();
  const q = $("search-input").value.trim();
  if (q) runSearch(q);
});
$("add-folder-btn").addEventListener("click", addFolder);
$("rescan-btn").addEventListener("click", async () => {
  try { await api("/api/rescan", { method: "POST" }); }
  catch (err) { $("status-line").textContent = err.message; }
});
$("settings-btn").addEventListener("click", openSettings);
$("close-settings-btn").addEventListener("click", () => $("settings-dialog").close());
$("reset-btn").addEventListener("click", resetLibrary);

refreshLibraryInfo();
startPolling();
