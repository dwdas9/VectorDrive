// Logs view: dedicated log viewer with filtering, auto-refresh, copy.
"use strict";

var _logsState = { lines: [], paused: false, filter: "", level: "all" };

function _filterLogLines(lines, filter, level) {
  return lines.filter(function (line) {
    if (level !== "all") {
      var upper = line.toUpperCase();
      if (level === "error" && upper.indexOf("ERROR") === -1) return false;
      if (level === "warning" && upper.indexOf("WARNING") === -1 && upper.indexOf("WARN") === -1) return false;
      if (level === "info" && upper.indexOf("INFO") === -1) return false;
    }
    if (filter) {
      return line.toLowerCase().indexOf(filter.toLowerCase()) !== -1;
    }
    return true;
  });
}

function _renderLogOutput(container, lines) {
  container.textContent = "";
  if (lines.length === 0) {
    container.textContent = "No log entries match the current filter.";
    return;
  }
  container.textContent = lines.join("\n");
  container.scrollTop = container.scrollHeight;
}

async function renderLogs(main) {
  var header = el("div", { class: "page-header" }, [
    el("h1", { text: "Logs" }),
  ]);
  main.appendChild(header);

  var filterInput = el("input", {
    type: "text",
    placeholder: "Filter logs…",
    "aria-label": "Filter logs",
    style: "flex:1;min-width:120px",
  });

  var levelSelect = el("select", { "aria-label": "Log level filter" }, [
    el("option", { value: "all", text: "All levels" }),
    el("option", { value: "error", text: "Errors only" }),
    el("option", { value: "warning", text: "Warnings" }),
    el("option", { value: "info", text: "Info" }),
  ]);

  var pauseBtn = el("button", { class: "secondary sm", text: _logsState.paused ? "Resume" : "Pause" });
  var copyBtn = el("button", { class: "secondary sm", text: "Copy" });
  var refreshBtn = el("button", { class: "secondary sm", text: "Refresh" });

  var toolbar = el("div", { class: "log-toolbar" }, [filterInput, levelSelect, pauseBtn, refreshBtn, copyBtn]);
  main.appendChild(toolbar);

  var output = el("pre", { class: "log-output", "aria-live": "off", "aria-label": "Log output" });
  main.appendChild(output);

  var statusBar = el("div", { style: "display:flex;justify-content:space-between;margin-top:8px;font-size:11px;color:var(--color-text-muted)" });
  var lineCount = el("span", { text: "" });
  var logSize = el("span", { text: "" });
  statusBar.appendChild(lineCount);
  statusBar.appendChild(logSize);
  main.appendChild(statusBar);

  function updateView() {
    var filtered = _filterLogLines(_logsState.lines, _logsState.filter, _logsState.level);
    _renderLogOutput(output, filtered);
    lineCount.textContent = filtered.length + " / " + _logsState.lines.length + " lines";
  }

  async function loadLogs() {
    var res = await api("settings_logs", 500);
    if (res.ok) {
      _logsState.lines = res.data.lines || [];
      logSize.textContent = "Log file: " + (res.data.total_bytes ? Math.round(res.data.total_bytes / 1024) + " KB" : "—");
      updateView();
    } else {
      output.textContent = "Could not load logs.";
    }
  }

  filterInput.addEventListener("input", function () {
    _logsState.filter = filterInput.value;
    updateView();
  });

  levelSelect.addEventListener("change", function () {
    _logsState.level = levelSelect.value;
    updateView();
  });

  pauseBtn.addEventListener("click", function () {
    _logsState.paused = !_logsState.paused;
    pauseBtn.textContent = _logsState.paused ? "Resume" : "Pause";
  });

  refreshBtn.addEventListener("click", loadLogs);

  copyBtn.addEventListener("click", function () {
    var filtered = _filterLogLines(_logsState.lines, _logsState.filter, _logsState.level);
    var text = filtered.join("\n");
    try {
      navigator.clipboard.writeText(text);
      toast("Copied " + filtered.length + " lines to clipboard.");
    } catch (e) {
      toast("Could not copy to clipboard.");
    }
  });

  var refreshInterval = setInterval(function () {
    if (!_logsState.paused && document.querySelector(".log-output")) {
      loadLogs();
    } else if (!document.querySelector(".log-output")) {
      clearInterval(refreshInterval);
    }
  }, 3000);

  await loadLogs();
}

VIEW_RENDERERS.logs = renderLogs;
