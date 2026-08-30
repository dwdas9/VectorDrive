// Global job-status card (v0.2 GUI makeover): a single floating card,
// mounted once in index.html and independent of the current route (the
// GuiApi/JobRunner backing it only ever runs one job at a time — see
// gui/jobs.py's module docstring). Shows an approximate ETA and, only
// while a job is active, system memory and Ollama's loaded-model memory
// footprint — so a long indexing run doesn't feel like an unbounded
// wait. Listens to the same `vd:event` CustomEvent app.js's poll loop
// already dispatches app-wide; no new transport.
"use strict";

const _jobLabels = {};

// Callers (Folders' index/retry buttons, Settings' backup/vacuum
// buttons) register a short label right after getting a job_id back, so
// the card can show "Indexing Documents/Foo" instead of a generic
// "Working…" — the generic JobRunner "started" event carries no label
// of its own. Safe to call before the "started" event is even polled:
// this is a plain in-memory map, read only when that event arrives.
function registerJobLabel(jobId, label) {
  _jobLabels[jobId] = label;
}

// Stage 8 GUI audit finding: views/settings.js's backup/vacuum buttons
// and views/claude.js's deep-test button all called a `_watchJob` that
// was defined nowhere in the codebase (a ReferenceError at click time,
// silent until this stage's audit — claude.js's own renderer was never
// wired into the app to surface it, and settings.js's throw was easy to
// miss without opening devtools). Same contract as folders.js's
// `_watchFolderJob`: listens for this one job's terminal `vd:event`
// (done/failed/cancelled) and calls onDone with the full event object
// (`{job_id, kind, payload, ...}`), onEvent for every event in between.
function _watchJob(jobId, onEvent, onDone) {
  const handler = (ev) => {
    if (ev.detail.job_id !== jobId) return;
    onEvent(ev.detail);
    if (["done", "failed", "cancelled"].includes(ev.detail.kind)) {
      window.removeEventListener("vd:event", handler);
      onDone(ev.detail);
    }
  };
  window.addEventListener("vd:event", handler);
}

(function () {
  let root = null;
  let state = null; // null whenever no job is active
  let resourceTimer = null;

  function _fmtBytes(n) {
    if (n === null || n === undefined) return null;
    if (n < 1024) return `${n} B`;
    const units = ["KB", "MB", "GB"];
    let v = n / 1024;
    let i = 0;
    while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
    return `${v.toFixed(1)} ${units[i]}`;
  }

  function _fmtEta(seconds) {
    if (seconds < 60) return `${Math.max(1, Math.round(seconds))}s`;
    return `${Math.round(seconds / 60)} min`;
  }

  function _stopResourcePolling() {
    if (resourceTimer !== null) {
      clearInterval(resourceTimer);
      resourceTimer = null;
    }
  }

  async function _pollResources() {
    if (!state) return; // job ended while this call was in flight
    const jobId = state.jobId;
    const res = await api("get_resource_usage");
    if (!state || state.jobId !== jobId) return; // a newer job may be active now
    if (res.ok) {
      state.memory = res.data.memory;
      state.ollamaBytes = res.data.ollama_model_bytes;
    }
    _render();
  }

  function _render() {
    if (!root) root = document.getElementById("job-status-root");
    if (!root) return;
    root.innerHTML = "";
    if (!state) return;

    const rows = [el("div", { style: "font-weight:600" }, [state.label])];

    if (state.total) {
      const frac = state.done / state.total;
      rows.push(el("p", { class: "job-status-row", text: `${state.done}/${state.total} files` }));
      rows.push(progressBar(frac));
    } else if (state.done > 0) {
      // Unknown total, but we've processed some files: show count without ETA
      rows.push(el("p", { class: "job-status-row", text: `${state.done} files processed` }));
      rows.push(el("p", { class: "job-status-row", text: "Processing…" }));
    } else {
      // No files processed yet and no total: generic message
      rows.push(el("p", { class: "job-status-row", text: "Processing…" }));
    }

    // "Estimating…" until enough samples exist for a meaningful
    // average — a projection from 1-2 files (wildly different OCR vs.
    // plain-text costs) is worse than admitting there's no estimate yet.
    // Only show ETA when we have a known total.
    if (state.total) {
      let etaText = "Estimating…";
      if (state.fileDurations.length >= 3) {
        const avg = state.fileDurations.reduce((a, b) => a + b, 0) / state.fileDurations.length;
        const remaining = Math.max(0, state.total - state.done) * avg;
        etaText = `~${_fmtEta(remaining)} remaining`;
      }
      rows.push(el("p", { class: "job-status-row", text: etaText }));
    }

    if (state.memory) {
      rows.push(el("p", {
        class: "job-status-row",
        text: `Memory: ${_fmtBytes(state.memory.used_bytes)} / ${_fmtBytes(state.memory.total_bytes)}`,
      }));
    }
    if (state.ollamaBytes) {
      rows.push(el("p", {
        class: "job-status-row",
        text: `Ollama model memory: ${_fmtBytes(state.ollamaBytes)}`,
      }));
    }

    if (state.cancellable) {
      if (state.pausing) {
        rows.push(el("p", {
          class: "job-status-row",
          text: "Pausing — finishing current file",
        }));
      }
      rows.push(el("button", {
        class: "secondary", text: "Pause", style: "margin-top:8px",
        disabled: state.pausing,
        onclick: async () => {
          state.pausing = true;
          _render();
          const res = await api("pause_job", state.jobId);
          if (!res.ok || !res.data.pausing) state.pausing = false;
          _render();
        },
      }));
    }

    root.appendChild(el("div", { class: "job-status-card" }, rows));
  }

  window.addEventListener("vd:event", (ev) => {
    const evt = ev.detail;
    if (!evt.job_id) return;

    if (evt.kind === "started") {
      state = {
        jobId: evt.job_id,
        label: _jobLabels[evt.job_id] || "Working…",
        done: 0,
        total: null,
        fileDurations: [],
        lastFileAt: null,
        memory: null,
        ollamaBytes: null,
        cancellable: false,
        pausing: false,
      };
      delete _jobLabels[evt.job_id];
      _stopResourcePolling(); // defensive: never more than one interval alive
      resourceTimer = setInterval(_pollResources, 2000);
      _pollResources();
      _render();
      return;
    }

    if (!state || evt.job_id !== state.jobId) return;

    if (evt.kind === "run_started") {
      state.total = evt.payload.total;
      state.done = evt.payload.completed || 0;
      state.cancellable = true;
      state.lastFileAt = Date.now();
    } else if (evt.kind === "file_done") {
      const now = Date.now();
      if (state.lastFileAt) state.fileDurations.push((now - state.lastFileAt) / 1000);
      state.lastFileAt = now;
      state.done = evt.payload.completed !== undefined
        ? evt.payload.completed : state.done + 1;
    } else if (evt.kind === "progress" && evt.payload && evt.payload.phase === "markdown_generation" && evt.payload.status === "started") {
      state.cancellable = true;
      state.lastFileAt = Date.now();
    } else if (
      evt.kind === "progress" && evt.payload && evt.payload.phase === "markdown_file" &&
      state.total === null
    ) {
      // Compatibility fallback for older Markdown workers. Current workers
      // emit exact run_started/file_done counters, so counting both events
      // would make the displayed progress exceed the total.
      const now = Date.now();
      if (state.lastFileAt) state.fileDurations.push((now - state.lastFileAt) / 1000);
      state.lastFileAt = now;
      state.done += 1;
    } else if (evt.kind === "done" || evt.kind === "failed" || evt.kind === "cancelled" || evt.kind === "paused") {
      if (evt.kind === "failed") {
        const message = evt.payload && evt.payload.error
          ? evt.payload.error
          : "The background job failed. Check Logs for details.";
        if (typeof toast === "function") toast(message);
      }
      // Requirement: every polling timer/background worker tied to a job
      // stops the moment that job ends, is cancelled, or the app closes
      // (the app-close case needs nothing further here — the whole JS
      // context is torn down with the window).
      _stopResourcePolling();
      state = null;
      _render();
      return;
    }
    _render();
  });
})();
