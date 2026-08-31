"use strict";

/*
 * Postmortem desktop shell.
 *
 * Talks to the Python backend exclusively through window.pywebview.api,
 * which does not exist until the 'pywebviewready' event fires (see the
 * listener at the bottom of this file). Nothing in here touches
 * window.pywebview before that event.
 *
 * Every bridge method resolves (never rejects) with either a plain
 * value (the pick_* dialogs) or a {ok: bool, ...} dict -- so the normal
 * error path everywhere below is "check result.ok", not try/catch. A
 * try/catch is still wrapped around each call as a defensive net for a
 * truly unexpected bridge failure, per api.py's own docs.
 */

// -- helpers shared with report/html.py & report/index.py's own inline
// scripts (same behavior, so numbers/text render identically whether
// they come from the shell or from a rendered report) ---------------

const esc = s => String(s ?? "").replace(/[&<>"]/g,
  c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

const num = n => n == null ? "?" :
  Math.abs(n) >= 1e9 ? (n / 1e9).toFixed(2) + "b" :
  Math.abs(n) >= 1e6 ? (n / 1e6).toFixed(2) + "m" :
  Math.abs(n) >= 1e3 ? (n / 1e3).toFixed(1) + "k" : Math.round(n).toString();

const mmss = s => {
  if (s == null) return "?";
  s = Math.round(s);
  const m = Math.floor(s / 60), sec = s % 60;
  return m >= 60
    ? `${Math.floor(m / 60)}:${String(m % 60).padStart(2, "0")}:${String(sec).padStart(2, "0")}`
    : `${m}:${String(sec).padStart(2, "0")}`;
};

function parseFloatOr(value, fallback) {
  const n = parseFloat(value);
  return Number.isFinite(n) ? n : fallback;
}

// Best-effort path join for suggesting an output filename inside a
// folder the user just picked via a native dialog. Not a robust path
// library -- just avoids "C:\foo" + "/bar.json" turning into a mixed
// separator string when a Windows folder was picked.
function joinPath(dir, filename) {
  if (!dir) return filename;
  const windowsStyle = dir.includes("\\") && !dir.includes("/");
  const sep = windowsStyle ? "\\" : "/";
  return dir.replace(/[\\/]+$/, "") + sep + filename;
}

function api() {
  return window.pywebview.api;
}

// -- banners ----------------------------------------------------------

function showBanner(el, message) {
  el.textContent = message;
  el.hidden = false;
}

function hideBanner(el) {
  el.hidden = true;
}

// -- busy overlays ------------------------------------------------------

function setBusy(overlayEl, busy, message) {
  if (message != null) {
    const span = overlayEl.querySelector("span:last-child");
    if (span) span.textContent = message;
  }
  overlayEl.hidden = !busy;
}

// -- screen routing -----------------------------------------------------

const SCREEN_IDS = ["home", "new", "history", "settings", "report"];

// Ephemeral (error/success) banners a prior screen visit may have left
// showing -- cleared on every navigation so a stale message from a
// previous action doesn't linger into an unrelated screen. home-hint is
// intentionally excluded: it's a persistent settings-driven hint, not a
// one-off action result.
const EPHEMERAL_BANNER_IDS = [
  "na-error-banner", "hist-error-banner",
  "set-error-banner", "set-success-banner", "extract-error-banner",
];

function showScreen(name) {
  for (const id of SCREEN_IDS) {
    const el = document.getElementById(`screen-${id}`);
    if (el) el.hidden = id !== name;
  }
  document.querySelectorAll(".navbtn").forEach(btn => {
    btn.classList.toggle("active", btn.dataset.screen === name);
  });
  for (const id of EPHEMERAL_BANNER_IDS) {
    document.getElementById(id)?.setAttribute("hidden", "");
  }
  document.getElementById("screen-" + name)?.scrollTo?.(0, 0);
}

function wireNav() {
  document.querySelectorAll("[data-screen]").forEach(btn => {
    btn.addEventListener("click", () => showScreen(btn.dataset.screen));
  });
  document.querySelectorAll("[data-nav]").forEach(btn => {
    btn.addEventListener("click", () => showScreen(btn.dataset.nav));
  });
}

// -- app state ------------------------------------------------------------

const state = {
  settings: null,
  lastReport: null, // the report dict behind the currently-shown report screen, if any -- stashed so "Upload to site" has something to send
};

function defaultSettings() {
  return {
    wow_addon_path: null,
    raiderio_region: null,
    avoidable_data_path: null,
    default_output_dir: null,
    history_db_path: null,
    site_url: null,
  };
}

// ===========================================================================
// Home
// ===========================================================================

function renderHomeHint() {
  const s = state.settings || defaultSettings();
  const hint = document.getElementById("home-hint");
  hint.hidden = !!(s.history_db_path || s.default_output_dir);
}

// ===========================================================================
// New Analysis
// ===========================================================================

const na = {}; // cached element refs, filled in initNewAnalysis()

function initNewAnalysis() {
  na.logPath = document.getElementById("na-log-path");
  na.pickLogBtn = document.getElementById("na-pick-log-btn");
  na.runField = document.getElementById("na-run-field");
  na.runSelect = document.getElementById("na-run-select");
  na.runsStatus = document.getElementById("na-runs-status");
  na.routeText = document.getElementById("na-route-text");
  na.routePickBtn = document.getElementById("na-route-pick-btn");
  na.dungeonDataPath = document.getElementById("na-dungeon-data-path");
  na.dungeonDataPickBtn = document.getElementById("na-dungeon-data-pick-btn");
  na.avoidableDataPath = document.getElementById("na-avoidable-data-path");
  na.avoidableDataPickBtn = document.getElementById("na-avoidable-data-pick-btn");
  na.raiderioRegion = document.getElementById("na-raiderio-region");
  na.pullGap = document.getElementById("na-pull-gap");
  na.deathPenalty = document.getElementById("na-death-penalty");
  na.analyzeBtn = document.getElementById("na-analyze-btn");
  na.errorBanner = document.getElementById("na-error-banner");
  na.busyOverlay = document.getElementById("na-busy-overlay");

  na.pickLogBtn.addEventListener("click", onPickLog);
  na.routePickBtn.addEventListener("click", onPickRouteFile);
  na.dungeonDataPickBtn.addEventListener("click", onPickDungeonData);
  na.avoidableDataPickBtn.addEventListener("click", onPickAvoidableData);
  na.analyzeBtn.addEventListener("click", onAnalyze);
}

function applySettingsToNewAnalysis() {
  const s = state.settings || defaultSettings();
  // Only raiderio_region and avoidable_data_path have a corresponding
  // saved-settings field (see desktop/config.py's DEFAULT_SETTINGS) --
  // there is no persisted "default route" or "default dungeon data
  // path", so those two fields start empty every session.
  if (s.avoidable_data_path) na.avoidableDataPath.value = s.avoidable_data_path;
  if (s.raiderio_region) na.raiderioRegion.value = s.raiderio_region;
}

function updateAnalyzeButtonState() {
  na.analyzeBtn.disabled = !na.logPath.value.trim();
}

async function onPickLog() {
  hideBanner(na.errorBanner);
  try {
    const path = await api().pick_log_file();
    if (!path) return;
    na.logPath.value = path;
    updateAnalyzeButtonState();
    await loadRuns(path);
  } catch (e) {
    showBanner(na.errorBanner, "Could not open the file picker: " + describeError(e));
  }
}

async function loadRuns(logPath) {
  na.runField.hidden = false;
  na.runSelect.disabled = true;
  na.runsStatus.textContent = "Loading runs…";
  // Reset to just the default option while loading.
  na.runSelect.innerHTML = "";
  na.runSelect.appendChild(defaultRunOption());

  try {
    const result = await api().list_runs(logPath);
    if (result && result.ok) {
      const runs = result.runs || [];
      for (const run of runs) {
        const opt = document.createElement("option");
        opt.value = String(run.index);
        opt.textContent = formatRunLabel(run);
        na.runSelect.appendChild(opt);
      }
      na.runsStatus.textContent = runs.length
        ? `${runs.length} run${runs.length === 1 ? "" : "s"} found. Defaults to the most recent.`
        : "No Mythic+ runs found in this log.";
    } else {
      na.runsStatus.textContent = "Could not list runs: " + ((result && result.error) || "unknown error") +
        ". You can still try analyzing the most recent run.";
    }
  } catch (e) {
    na.runsStatus.textContent = "Could not list runs: " + describeError(e);
  } finally {
    na.runSelect.disabled = false;
  }
}

function defaultRunOption() {
  const opt = document.createElement("option");
  opt.value = "last";
  opt.textContent = "Most recent run in this log";
  opt.selected = true;
  return opt;
}

function formatRunLabel(run) {
  const zone = run.zone || "Unknown dungeon";
  const level = run.keystone_level != null ? `+${run.keystone_level}` : "+?";
  let outcome;
  if (!run.completed) outcome = "incomplete";
  else if (run.timed === true) outcome = "timed";
  else if (run.timed === false) outcome = "over timer";
  else outcome = "completed";
  const length = run.wall_duration_s != null ? mmss(run.wall_duration_s) : "?";
  const when = run.start_ts ? new Date(run.start_ts * 1000).toLocaleString() : "";
  return `#${run.index} — ${zone} ${level} — ${outcome} — ${length}` + (when ? ` — ${when}` : "");
}

async function onPickRouteFile() {
  try {
    const path = await api().pick_route_file();
    if (path) na.routeText.value = path;
  } catch (e) {
    showBanner(na.errorBanner, "Could not open the file picker: " + describeError(e));
  }
}

async function onPickDungeonData() {
  try {
    const path = await api().pick_dungeon_data_file();
    if (path) na.dungeonDataPath.value = path;
  } catch (e) {
    showBanner(na.errorBanner, "Could not open the file picker: " + describeError(e));
  }
}

async function onPickAvoidableData() {
  try {
    const path = await api().pick_avoidable_data_file();
    if (path) na.avoidableDataPath.value = path;
  } catch (e) {
    showBanner(na.errorBanner, "Could not open the file picker: " + describeError(e));
  }
}

async function onAnalyze() {
  const logPath = na.logPath.value.trim();
  if (!logPath) return;

  hideBanner(na.errorBanner);
  na.analyzeBtn.disabled = true;
  setBusy(na.busyOverlay, true);

  const params = {
    log_path: logPath,
    run_selector: na.runSelect.value || "last",
    pull_gap_seconds: parseFloatOr(na.pullGap.value, 5.0),
    death_penalty_s: parseFloatOr(na.deathPenalty.value, 15.0),
  };
  const route = na.routeText.value.trim();
  if (route) params.route = route;
  const dungeonData = na.dungeonDataPath.value.trim();
  if (dungeonData) params.dungeon_data_path = dungeonData;
  const avoidableData = na.avoidableDataPath.value.trim();
  if (avoidableData) params.avoidable_data_path = avoidableData;
  const region = na.raiderioRegion.value;
  if (region) params.raiderio_region = region;

  try {
    const result = await api().analyze(params);
    if (result && result.ok) {
      const run = (result.report && result.report.run) || {};
      const label = [run.zone, run.keystone_level != null ? `+${run.keystone_level}` : null]
        .filter(Boolean).join(" ");
      document.getElementById("report-context-label").textContent = label;
      const frame = document.getElementById("report-frame");
      frame.srcdoc = result.html;
      state.lastReport = result.report;
      resetUploadStatus();
      showScreen("report");
    } else {
      showBanner(na.errorBanner, (result && result.error) || "Analysis failed for an unknown reason.");
    }
  } catch (e) {
    showBanner(na.errorBanner, "Unexpected error while analyzing: " + describeError(e));
  } finally {
    setBusy(na.busyOverlay, false);
    updateAnalyzeButtonState();
  }
}

function initReportScreen() {
  document.getElementById("report-back-btn").addEventListener("click", () => showScreen("new"));
  document.getElementById("report-upload-btn").addEventListener("click", onUploadToSite);
}

function resetUploadStatus() {
  const status = document.getElementById("report-upload-status");
  status.hidden = true;
  status.classList.remove("ok", "err");
  status.textContent = "";
}

async function onUploadToSite() {
  if (!state.lastReport) return;
  const btn = document.getElementById("report-upload-btn");
  const status = document.getElementById("report-upload-status");

  btn.disabled = true;
  status.hidden = false;
  status.classList.remove("ok", "err");
  status.textContent = "Uploading…";

  try {
    const result = await api().upload_report(state.lastReport);
    if (result && result.ok) {
      status.classList.add("ok");
      status.textContent = `Uploaded — ${result.url || "see the site"}`;
    } else {
      status.classList.add("err");
      status.textContent = (result && result.error) || "Upload failed for an unknown reason.";
    }
  } catch (e) {
    status.classList.add("err");
    status.textContent = "Unexpected error while uploading: " + describeError(e);
  } finally {
    btn.disabled = false;
  }
}

// ===========================================================================
// History
// ===========================================================================

const hist = {};

function initHistory() {
  hist.dbPath = document.getElementById("hist-db-path");
  hist.directory = document.getElementById("hist-directory");
  hist.pickFolderBtn = document.getElementById("hist-pick-folder-btn");
  hist.loadBtn = document.getElementById("hist-load-btn");
  hist.errorBanner = document.getElementById("hist-error-banner");
  hist.busyOverlay = document.getElementById("hist-busy-overlay");
  hist.formView = document.getElementById("hist-form-view");
  hist.resultsView = document.getElementById("hist-results-view");
  hist.backBtn = document.getElementById("hist-back-btn");
  hist.frame = document.getElementById("hist-frame");

  hist.pickFolderBtn.addEventListener("click", onPickHistoryFolder);
  hist.loadBtn.addEventListener("click", onLoadHistory);
  hist.backBtn.addEventListener("click", () => {
    hist.resultsView.hidden = true;
    hist.formView.hidden = false;
  });
}

function applySettingsToHistory() {
  const s = state.settings || defaultSettings();
  if (s.history_db_path) hist.dbPath.value = s.history_db_path;
}

async function onPickHistoryFolder() {
  try {
    const folder = await api().pick_folder("Choose a reports folder");
    if (folder) hist.directory.value = folder;
  } catch (e) {
    showBanner(hist.errorBanner, "Could not open the folder picker: " + describeError(e));
  }
}

async function onLoadHistory() {
  const dbPath = hist.dbPath.value.trim();
  const directory = hist.directory.value.trim();
  hideBanner(hist.errorBanner);

  if (!dbPath && !directory) {
    showBanner(hist.errorBanner, "Enter a history database path or choose a reports folder first.");
    return;
  }

  hist.loadBtn.disabled = true;
  setBusy(hist.busyOverlay, true);
  try {
    const result = await api().list_history(dbPath || null, directory || null);
    if (result && result.ok) {
      hist.frame.srcdoc = result.html;
      hist.formView.hidden = true;
      hist.resultsView.hidden = false;
    } else {
      showBanner(hist.errorBanner, (result && result.error) || "Could not load history.");
    }
  } catch (e) {
    showBanner(hist.errorBanner, "Unexpected error while loading history: " + describeError(e));
  } finally {
    hist.loadBtn.disabled = false;
    setBusy(hist.busyOverlay, false);
  }
}

// ===========================================================================
// Settings
// ===========================================================================

const set = {};

function initSettings() {
  set.wowAddonPath = document.getElementById("set-wow-addon-path");
  set.wowAddonPickBtn = document.getElementById("set-wow-addon-pick-btn");
  set.raiderioRegion = document.getElementById("set-raiderio-region");
  set.avoidableDataPath = document.getElementById("set-avoidable-data-path");
  set.avoidableDataPickBtn = document.getElementById("set-avoidable-data-pick-btn");
  set.defaultOutputDir = document.getElementById("set-default-output-dir");
  set.defaultOutputDirPickBtn = document.getElementById("set-default-output-dir-pick-btn");
  set.historyDbPath = document.getElementById("set-history-db-path");
  set.siteUrl = document.getElementById("set-site-url");
  set.saveBtn = document.getElementById("set-save-btn");
  set.errorBanner = document.getElementById("set-error-banner");
  set.successBanner = document.getElementById("set-success-banner");
  set.busyOverlay = document.getElementById("set-busy-overlay");

  set.extractOutputPath = document.getElementById("extract-output-path");
  set.extractOutputPickBtn = document.getElementById("extract-output-pick-btn");
  set.extractBtn = document.getElementById("extract-btn");
  set.extractDisabledHint = document.getElementById("extract-disabled-hint");
  set.extractErrorBanner = document.getElementById("extract-error-banner");
  set.extractSummary = document.getElementById("extract-summary");
  set.extractCountLine = document.getElementById("extract-count-line");
  set.extractDungeonRows = document.getElementById("extract-dungeon-rows");

  set.wowAddonPickBtn.addEventListener("click", onPickWowAddonFolder);
  set.avoidableDataPickBtn.addEventListener("click", onPickSettingsAvoidableData);
  set.defaultOutputDirPickBtn.addEventListener("click", onPickDefaultOutputDir);
  set.saveBtn.addEventListener("click", onSaveSettings);

  set.wowAddonPath.addEventListener("input", updateExtractButtonState);
  set.extractOutputPickBtn.addEventListener("click", onPickExtractOutputFolder);
  set.extractBtn.addEventListener("click", onExtractDungeonData);
}

function applySettingsToForm() {
  const s = state.settings || defaultSettings();
  set.wowAddonPath.value = s.wow_addon_path || "";
  set.raiderioRegion.value = s.raiderio_region || "";
  set.avoidableDataPath.value = s.avoidable_data_path || "";
  set.defaultOutputDir.value = s.default_output_dir || "";
  set.historyDbPath.value = s.history_db_path || "";
  set.siteUrl.value = s.site_url || "";
  updateExtractButtonState();
}

function updateExtractButtonState() {
  const hasAddon = !!set.wowAddonPath.value.trim();
  set.extractBtn.disabled = !hasAddon;
  set.extractDisabledHint.hidden = hasAddon;
}

async function onPickWowAddonFolder() {
  try {
    const folder = await api().pick_folder("Choose your Mythic Dungeon Tools addon folder");
    if (folder) {
      set.wowAddonPath.value = folder;
      updateExtractButtonState();
    }
  } catch (e) {
    showBanner(set.errorBanner, "Could not open the folder picker: " + describeError(e));
  }
}

async function onPickSettingsAvoidableData() {
  try {
    const path = await api().pick_avoidable_data_file();
    if (path) set.avoidableDataPath.value = path;
  } catch (e) {
    showBanner(set.errorBanner, "Could not open the file picker: " + describeError(e));
  }
}

async function onPickDefaultOutputDir() {
  try {
    const folder = await api().pick_folder("Choose default output folder");
    if (folder) set.defaultOutputDir.value = folder;
  } catch (e) {
    showBanner(set.errorBanner, "Could not open the folder picker: " + describeError(e));
  }
}

async function onSaveSettings() {
  hideBanner(set.errorBanner);
  hideBanner(set.successBanner);
  setBusy(set.busyOverlay, true, "Saving…");
  set.saveBtn.disabled = true;

  const payload = {
    wow_addon_path: set.wowAddonPath.value.trim() || null,
    raiderio_region: set.raiderioRegion.value || null,
    avoidable_data_path: set.avoidableDataPath.value.trim() || null,
    default_output_dir: set.defaultOutputDir.value.trim() || null,
    history_db_path: set.historyDbPath.value.trim() || null,
    site_url: set.siteUrl.value.trim() || null,
  };

  try {
    const result = await api().save_settings(payload);
    if (result && result.ok) {
      state.settings = { ...defaultSettings(), ...payload };
      showBanner(set.successBanner, "Settings saved.");
      renderHomeHint();
    } else {
      showBanner(set.errorBanner, (result && result.error) || "Could not save settings.");
    }
  } catch (e) {
    showBanner(set.errorBanner, "Unexpected error while saving: " + describeError(e));
  } finally {
    setBusy(set.busyOverlay, false);
    set.saveBtn.disabled = false;
  }
}

async function onPickExtractOutputFolder() {
  try {
    const folder = await api().pick_folder("Choose output folder for dungeon data");
    if (folder) set.extractOutputPath.value = joinPath(folder, "mdt_data.json");
  } catch (e) {
    showBanner(set.extractErrorBanner, "Could not open the folder picker: " + describeError(e));
  }
}

async function onExtractDungeonData() {
  const addonPath = set.wowAddonPath.value.trim();
  const outputPath = set.extractOutputPath.value.trim();
  hideBanner(set.extractErrorBanner);
  set.extractSummary.hidden = true;

  if (!addonPath) return;
  if (!outputPath) {
    showBanner(set.extractErrorBanner, "Choose an output file first.");
    return;
  }

  set.extractBtn.disabled = true;
  setBusy(set.busyOverlay, true, "Extracting dungeon data…");
  try {
    const result = await api().extract_dungeon_data(addonPath, outputPath);
    if (result && result.ok) {
      set.extractCountLine.textContent =
        `Extracted ${num(result.dungeon_count)} dungeon${result.dungeon_count === 1 ? "" : "s"} to ${result.output_path}`;
      set.extractDungeonRows.innerHTML = (result.dungeons || []).map(d => `<tr>
        <td>${esc(d.name)}</td>
        <td class="num">${num(d.enemy_count)}</td>
      </tr>`).join("");
      set.extractSummary.hidden = false;
    } else {
      showBanner(set.extractErrorBanner, (result && result.error) || "Extraction failed for an unknown reason.");
    }
  } catch (e) {
    showBanner(set.extractErrorBanner, "Unexpected error while extracting: " + describeError(e));
  } finally {
    setBusy(set.busyOverlay, false);
    updateExtractButtonState();
  }
}

// ===========================================================================
// Boot
// ===========================================================================

function describeError(e) {
  return (e && e.message) ? e.message : String(e);
}

async function boot() {
  initNewAnalysis();
  initHistory();
  initSettings();
  initReportScreen();
  wireNav();

  try {
    state.settings = await api().get_settings();
  } catch (e) {
    state.settings = defaultSettings();
  }

  applySettingsToNewAnalysis();
  applySettingsToHistory();
  applySettingsToForm();
  renderHomeHint();

  document.getElementById("boot-loading").hidden = true;
  showScreen("home");
}

window.addEventListener("pywebviewready", boot);
