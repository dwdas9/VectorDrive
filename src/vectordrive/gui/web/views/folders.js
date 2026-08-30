// Folders view: dense data table with right inspector panel.
"use strict";

function _fmtBytesF(n) {
  if (!n) return "—";
  if (n < 1024) return n + " B";
  var units = ["KB", "MB", "GB"];
  var v = n / 1024, i = 0;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  return v.toFixed(1) + " " + units[i];
}

function _folderBasename(path) {
  var parts = path.replace(/\/$/, "").split("/");
  return parts[parts.length - 1] || path;
}

function _statusBadge(f) {
  if (!f.exists) return el("span", { class: "status-badge error", text: "Folder not found" });
  if (f._indexing) return el("span", { class: "status-badge indexing", text: "Indexing…" });
  if (f.failed > 0) return el("span", { class: "status-badge error", text: "Errors" });
  if (f.warnings > 0) return el("span", { class: "status-badge paused", text: "Warnings" });
  if (f.indexed > 0) return el("span", { class: "status-badge up-to-date", text: "Indexed" });
  return el("span", { class: "status-badge cancelled", text: "Empty" });
}

var _foldersState = {
  selected: null,
  folders: [],
  inspectorCollapsed: false,
  outputMarkdownFiles: true,
  progress: {},
  rerenderInspector: null,
  ocrModels: [],
  selectedOcrModel: null,
  ocrModelError: null,
};

function _setFolderProgress(path, update) {
  _foldersState.progress[path] = Object.assign(
    { phase: "index", done: 0, total: null },
    _foldersState.progress[path] || {},
    update || {}
  );
  if (_foldersState.rerenderInspector) _foldersState.rerenderInspector();
}

function _clearFolderProgress(path) {
  delete _foldersState.progress[path];
  if (_foldersState.rerenderInspector) _foldersState.rerenderInspector();
}

function _trackFolderJobEvent(folderPath, detail) {
  var payload = detail.payload || {};
  var phase = payload.workflow_phase || payload.phase;
  if (detail.kind === "run_started") {
    _setFolderProgress(folderPath, {
      phase: phase || "index",
      done: payload.completed || 0,
      total: payload.total === undefined ? null : payload.total,
    });
  } else if (detail.kind === "file_done") {
    var current = _foldersState.progress[folderPath] || {};
    _setFolderProgress(folderPath, {
      phase: phase || current.phase || "index",
      done: payload.completed === undefined ? (current.done || 0) + 1 : payload.completed,
      total: payload.total === undefined ? current.total || null : payload.total,
    });
  }
}

function _watchFolderJob(jobId, onEvent, onDone) {
  var finished = false;

  var handler = function (ev) {
    if (ev.detail.job_id !== jobId) return;
    onEvent(ev.detail);
    if (["done", "failed", "cancelled"].includes(ev.detail.kind) || ev.detail.kind === "paused") {
      if (!finished) {
        finished = true;
        window.removeEventListener("vd:event", handler);
        // Normalize event to match get_job shape: {status, result, error, error_code}
        var normalized = {
          status: ev.detail.kind,
          result: ev.detail.payload ? ev.detail.payload.result : null,
          error: ev.detail.payload ? ev.detail.payload.error : null,
          error_code: ev.detail.payload ? ev.detail.payload.error_code : null,
        };
        onDone(normalized);
      }
    }
  };

  // Register listener FIRST before any API calls
  window.addEventListener("vd:event", handler);

  // Then immediately get authoritative status to handle race where job finishes before listener registers
  api("get_job", jobId).then(function (res) {
    if (!res.ok) return;
    var status = res.data.status;
    if (["done", "failed", "cancelled"].includes(status) || status === "paused") {
      // Job already terminal; finish if event hasn't arrived yet
      if (!finished) {
        finished = true;
        window.removeEventListener("vd:event", handler);
        onDone(res.data);
      }
    }
  });
}

function _renderInspector(container, folder, reload, inspectorCtx) {
  container.innerHTML = "";
  inspectorCtx = inspectorCtx || { collapsed: false, setCollapsed: function () {} };
  var toggle = el("button", {
    class: "folder-inspector-toggle",
    type: "button",
    title: inspectorCtx.collapsed ? "Expand inspector" : "Collapse inspector",
    "aria-label": inspectorCtx.collapsed ? "Expand folder inspector" : "Collapse folder inspector",
    "aria-expanded": inspectorCtx.collapsed ? "false" : "true",
    text: inspectorCtx.collapsed ? "‹" : "›",
    onclick: function () { inspectorCtx.setCollapsed(!inspectorCtx.collapsed); },
  });
  if (inspectorCtx.collapsed) {
    container.appendChild(toggle);
    return;
  }
  var heading = el("h2", {
    class: "folder-inspector-heading",
    tabindex: "-1",
    text: folder ? _folderBasename(folder.path) : "Folder details",
  });
  container.appendChild(el("div", { class: "folder-inspector-header" }, [heading, toggle]));
  var body = el("div", { class: "inspector-body" });
  container.appendChild(body);
  if (!folder) {
    body.appendChild(el("p", { text: "Select a folder to view details.", style: "color:var(--color-text-muted);padding:var(--space-4)" }));
    return;
  }

  if (!folder.exists) {
    body.appendChild(el("div", { class: "inspector-section" }, [
      el("h3", { text: _folderBasename(folder.path) }),
      el("p", { text: "Folder not found", style: "font-weight:600;color:var(--color-danger)" }),
      el("p", { class: "mono-path", text: "Last known location: " + folder.path }),
      el("p", { text: "The folder may have been renamed, moved, or be temporarily unavailable. Its index is preserved.", style: "color:var(--color-text-muted)" }),
      el("button", { text: "Relink Folder…", onclick: function () { _relinkFlow(folder, reload); } }),
    ]));
    return;
  }

  body.appendChild(el("div", { class: "inspector-section" }, [
    el("h3", { text: "Stats" }),
    el("ul", { class: "stat-list" }, [
      el("li", {}, [el("span", { class: "stat-label", text: "Indexed" }), el("span", { class: "stat-value", text: String(folder.indexed) })]),
      el("li", {}, [el("span", { class: "stat-label", text: "Failed" }), el("span", { class: "stat-value", text: String(folder.failed) })]),
      el("li", {}, [el("span", { class: "stat-label", text: "Warnings" }), el("span", { class: "stat-value", text: String(folder.warnings || 0) })]),
      el("li", {}, [el("span", { class: "stat-label", text: "Size" }), el("span", { class: "stat-value", text: _fmtBytesF(folder.total_bytes) })]),
      el("li", {}, [el("span", { class: "stat-label", text: "Last indexed" }), el("span", { class: "stat-value", text: folder.last_indexed_at || "Never" })]),
    ]),
  ]));

  var modelSection = el("div", { class: "inspector-section" }, [
    el("h3", { text: "OCR model" }),
  ]);
  if (_foldersState.ocrModels.length > 0) {
    var modelSelect = el("select", {
      id: "folder-ocr-model",
      class: "folder-ocr-model",
      "aria-label": "OCR model",
    });
    if (!_foldersState.selectedOcrModel) {
      var placeholder = el("option", {
        value: "",
        text: "Choose an installed OCR model",
        disabled: "disabled",
      });
      placeholder.selected = true;
      modelSelect.appendChild(placeholder);
    }
    _foldersState.ocrModels.forEach(function (model) {
      var option = el("option", { value: model.name, text: model.name });
      if (model.name === _foldersState.selectedOcrModel) option.selected = true;
      modelSelect.appendChild(option);
    });
    modelSelect.addEventListener("change", async function () {
      modelSelect.disabled = true;
      var res = await api("set_ocr_model", modelSelect.value);
      modelSelect.disabled = false;
      if (res.ok) {
        _foldersState.selectedOcrModel = res.data.selected_model;
        toast("OCR model set to " + res.data.selected_model + ".");
      } else {
        toast((res.error && res.error.message) || "Could not change the OCR model.");
        modelSelect.value = _foldersState.selectedOcrModel || "";
      }
    });
    modelSection.appendChild(modelSelect);
    modelSection.appendChild(el("p", {
      class: "folder-mode-help",
      text: "Only curated Qwen vision models already installed in local Ollama are shown.",
    }));
  } else {
    modelSection.appendChild(el("p", {
      class: "folder-mode-help",
      text: _foldersState.ocrModelError ||
        "No curated Qwen 4B or 8B vision model is currently installed.",
    }));
  }
  body.appendChild(modelSection);

  var progress = _foldersState.progress[folder.path];
  if (progress) {
    var progressLabel = progress.phase === "markdown" ? "Creating Markdown" : "Indexing";
    var progressText = progress.total === null
      ? String(progress.done) + " processed"
      : String(progress.done) + "/" + String(progress.total);
    var progressRows = [
      el("h3", { text: "Processing" }),
      el("p", {
        class: "folder-progress-count",
        role: "status",
        "aria-live": "polite",
        text: progressLabel + " · " + progressText,
      }),
    ];
    if (progress.total > 0) progressRows.push(progressBar(progress.done / progress.total));
    body.appendChild(el("div", { class: "inspector-section" }, progressRows));
  }

  body.appendChild(el("div", { class: "inspector-section" }, [
    el("h3", { text: "Path" }),
    el("p", { class: "mono-path", text: folder.path }),
  ]));

  var markdownToggle = el("input", {
    id: "folder-output-markdown",
    type: "checkbox",
  });
  markdownToggle.checked = _foldersState.outputMarkdownFiles;
  markdownToggle.addEventListener("change", function () {
    _foldersState.outputMarkdownFiles = markdownToggle.checked;
    api("save_ui_state", { output_markdown_files: markdownToggle.checked });
    if (_foldersState.rerenderInspector) _foldersState.rerenderInspector();
  });
  body.appendChild(el("div", { class: "inspector-section" }, [
    el("h3", { text: "Processing mode" }),
    el("label", { class: "folder-markdown-toggle", for: "folder-output-markdown" }, [
      markdownToggle,
      el("span", { text: "Output Markdown files" }),
    ]),
    el("p", {
      class: "folder-mode-help",
      text: _foldersState.outputMarkdownFiles
        ? "Index creates inspectable *.vectordrive.md files first, then indexes them."
        : "Index processes source files directly and does not create Markdown sidecars.",
    }),
  ]));

  body.appendChild(el("div", { class: "inspector-section" }, [
    el("h3", { text: "How to use" }),
    el("p", { text: "Index follows the processing mode above. Markdown output keeps inspectable extracted-text artifacts next to each source; direct mode keeps those artifacts only inside VectorDrive.", style: "color:var(--color-text-muted)" }),
  ]));

  if (folder.is_cloud_synced) {
    body.appendChild(el("div", { class: "inspector-section" }, [
      el("h3", { text: "Notice" }),
      el("p", { text: "This folder appears to be cloud-synced. Files not downloaded locally may be skipped.", style: "color:var(--color-warning)" }),
    ]));
  }

  if (folder.warnings > 0) {
    var warnSection = el("div", { class: "inspector-section" });
    warnSection.appendChild(el("h3", { text: "Warnings" }));
    var warnList = el("div", { class: "live-log" });
    warnList.appendChild(el("p", { text: "Loading…", style: "color:var(--color-text-muted)" }));
    warnSection.appendChild(warnList);
    body.appendChild(warnSection);

    api("get_folder_warnings", folder.path).then(function (res) {
      warnList.innerHTML = "";
      if (res.ok && res.data.length > 0) {
        res.data.forEach(function (w) {
          warnList.appendChild(el("div", { class: "live-log-entry error" }, [
            el("span", { class: "msg", text: w.path.split("/").pop() + ": " + w.warning }),
          ]));
        });
      } else {
        warnList.appendChild(el("p", { text: "No details available.", style: "color:var(--color-text-muted)" }));
      }
    });
  }
}

function _renderResumableRuns(contentArea, runs, reload) {
  // A live folder job owns its root path.  Do not render a stale durable-run
  // Resume action beside that job; the global job card is authoritative.
  runs = (runs || []).filter(function (run) {
    return !_foldersState.progress[run.root_path];
  });
  if (runs.length === 0) return;
  var card = el("div", { class: "card", style: "margin-bottom:var(--space-4)" });
  card.appendChild(el("h2", { text: "Paused work" }));
  card.appendChild(el("p", {
    text: "Completed files are saved. Resume continues from the next safe file/page boundary.",
    style: "color:var(--color-text-muted)",
  }));
  runs.forEach(function (run) {
    var label = run.phase === "markdown" ? "Markdown" : "Indexing";
    var row = el("div", { style: "display:flex;align-items:center;gap:12px;margin-top:8px" }, [
      el("span", {
        text: label + " · " + run.completed_items + "/" + run.total_items + " · " + truncateMiddle(run.root_path, 48),
        title: run.root_path,
        style: "flex:1",
      }),
      el("button", { class: "sm", text: "Resume", onclick: async function () {
      var res = run.phase === "markdown"
        ? await api("start_markdown_generation", run.root_path, run.run_id)
        : await api("start_index", run.root_path, "incremental", run.run_id);
        if (!res.ok) {
          toast(res.error ? res.error.message : "Could not resume this run.");
          return;
        }
        var jobId = res.data.job_id;
        _setFolderProgress(run.root_path, {
          phase: run.phase === "markdown" ? "markdown" : "index",
          done: run.completed_items,
          total: run.total_items,
        });
        registerJobLabel(jobId, "Resuming " + label.toLowerCase() + " for " + _folderBasename(run.root_path));
        _watchFolderJob(jobId, function (detail) {
          _trackFolderJobEvent(run.root_path, detail);
        }, function () {
          _clearFolderProgress(run.root_path);
          reload();
        });
        reload();
      }}),
    ]);
    card.appendChild(row);
  });
  contentArea.appendChild(card);
}

function _renderFoldersTable(contentArea, inspector, folders, resumableRuns, reload, inspectorCtx) {
  contentArea.innerHTML = "";

  var header = el("div", { class: "page-header" }, [
    el("h1", { text: "Folders" }),
    el("div", { class: "page-header-actions" }, [
      el("button", { text: "+ Add Folder", "data-action": "add-folder", onclick: function () { _addFolderFlow(reload); } }),
    ]),
  ]);
  contentArea.appendChild(header);
  _renderResumableRuns(contentArea, resumableRuns, reload);
  var pausedRoots = {};
  (resumableRuns || []).forEach(function (run) { pausedRoots[run.root_path] = true; });

  if (folders.length === 0) {
    contentArea.appendChild(renderEmpty({
      title: "No folders added yet.",
      detail: "Add a folder to start indexing PDFs, DOCX, text, Markdown and images.",
      actionLabel: "Add Folder",
      onAction: function () { _addFolderFlow(reload); },
    }));
    _renderInspector(inspector, null, reload, inspectorCtx);
    return;
  }

  var table = el("table", { class: "data-table" });
  var thead = el("thead", {}, [
    el("tr", {}, [
      el("th", { text: "Folder" }),
      el("th", { class: "num", text: "Files" }),
      el("th", { class: "num", text: "Indexed" }),
      el("th", { class: "num", text: "Errors" }),
      el("th", { class: "num", text: "Warnings" }),
      el("th", { text: "Status" }),
      el("th", { class: "actions-cell", text: "Actions" }),
    ]),
  ]);
  table.appendChild(thead);

  var tbody = el("tbody", {});
  folders.forEach(function (f) {
    var row = el("tr", { onclick: function () {
      _foldersState.selected = f.path;
      tbody.querySelectorAll("tr").forEach(function (r) { r.classList.remove("selected"); });
      row.classList.add("selected");
      _renderInspector(inspector, f, reload, inspectorCtx);
    }});
    if (_foldersState.selected === f.path) row.classList.add("selected");

    row.appendChild(el("td", {}, [
      el("div", { class: "folder-cell" }, [
        el("span", { class: "folder-name", text: _folderBasename(f.path) }),
        el("span", { class: "folder-path", text: truncateMiddle(f.path, 50) }),
      ]),
    ]));
    var totalFiles = (f.indexed || 0) + (f.failed || 0) + (f.unsupported || 0);
    row.appendChild(el("td", { class: "num", text: String(totalFiles) }));
    row.appendChild(el("td", { class: "num", text: String(f.indexed) }));
    row.appendChild(el("td", { class: "num" }, [
      f.failed > 0
        ? el("span", { class: "count-pill errors", text: String(f.failed) })
        : document.createTextNode("0"),
    ]));
    row.appendChild(el("td", { class: "num" }, [
      f.warnings > 0
        ? el("span", { class: "count-pill warnings", text: String(f.warnings) })
        : document.createTextNode("0"),
    ]));
    row.appendChild(el("td", {}, [_statusBadge(f)]));

    var actions = el("td", { class: "actions-cell" });
    var activeProgress = _foldersState.progress[f.path];
    var pausedWork = !!pausedRoots[f.path];
    var removeDialog = function () {
      confirmDialog({
        title: "Remove folder",
        body: f.exists
          ? "This deletes VectorDrive’s index for this folder. Your original files are never touched."
          : "This folder can't be found on disk. Removing it deletes VectorDrive's saved index for it.",
        confirmLabel: "Remove",
        danger: true,
        onConfirm: async function () {
          var res = await api("remove_folder", f.path, true);
          if (res.ok) { _foldersState.selected = null; reload(); }
        },
      });
    };
    if (f.exists) {
      actions.appendChild(el("button", { class: "sm", text: activeProgress ? "Indexing…" : (pausedWork ? "Resume paused work" : (f.has_index_history ? "Re-index" : "Index")), disabled: !!activeProgress || pausedWork, title: activeProgress ? "This folder is already being processed." : (pausedWork ? "Resume the paused run above before starting a new index." : ""), onclick: function (e) {
        if (activeProgress || pausedWork) return;
        e.stopPropagation();
        _startFolderIndex(f, "incremental", reload);
      }}));
      var items = [];
      if (f.failed > 0) {
        items.push({ label: "Retry Failed Files", onClick: function () { _startFolderIndex(f, "retry", reload); } });
      }
      items.push({ label: "Relink Folder…", onClick: function () { _relinkFlow(f, reload); } });
      items.push("-");
      items.push({
        label: "Full Rebuild…",
        onClick: function () {
          confirmDialog({
            title: "Full Rebuild",
            body: "Discards this folder's saved index and reprocesses every file from scratch. This can take a long time.",
            confirmLabel: "Full Rebuild",
            danger: true,
            onConfirm: function () { _startFolderIndex(f, "rebuild", reload); },
          });
        },
      });
      items.push({ label: "Remove Folder…", danger: true, onClick: removeDialog });
      actions.appendChild(menuButton(items));
    } else {
      actions.appendChild(el("button", { class: "sm", text: "Relink Folder…", onclick: function (e) {
        e.stopPropagation();
        _relinkFlow(f, reload);
      }}));
      actions.appendChild(menuButton([{ label: "Remove Folder…", danger: true, onClick: removeDialog }]));
    }
    row.appendChild(actions);
    tbody.appendChild(row);
  });
  table.appendChild(tbody);
  contentArea.appendChild(table);

  var selectedFolder = folders.find(function (f) { return f.path === _foldersState.selected; });
  _renderInspector(inspector, selectedFolder || null, reload, inspectorCtx);
}

function _relinkFlow(folder, reload) {
  (async function () {
    var chosen = await api("choose_folder");
    if (!chosen.ok || !chosen.data.path) return;
    var picked = chosen.data.path;

    var validation = await api("validate_relink", folder.path, picked);
    if (!validation.ok) {
      toast(validation.error ? validation.error.message : "Could not validate that folder.");
      return;
    }
    var v = validation.data;
    var basename = _folderBasename(folder.path);

    var doRelink = async function () {
      var res = await api("relink_folder", folder.path, picked);
      if (res.ok) { reload(); } else { toast(res.error ? res.error.message : "Could not relink folder."); }
    };

    if (v.ok) {
      confirmDialog({
        title: "Relink folder",
        body: "VectorDrive will treat “" + truncateMiddle(picked, 50) + "” as the new location of " + basename + ". Your index is kept.",
        confirmLabel: "Relink",
        onConfirm: doRelink,
      });
    } else {
      confirmDialog({
        title: "Relink folder",
        body: v.message,
        confirmLabel: "Relink Anyway",
        danger: true,
        onConfirm: doRelink,
      });
    }
  })();
}

function _startMarkdownGeneration(folder, reload) {
  toast("Creating Markdown files…");
  _setFolderProgress(folder.path, { phase: "markdown", done: 0, total: null });

  api("start_markdown_generation", folder.path).then(function (res) {
    if (!res.ok) {
      toast("Failed to start Markdown creation.");
      return;
    }
    var jobId = res.data.job_id;
    registerJobLabel(jobId, "Creating Markdown for " + _folderBasename(folder.path));
    _watchFolderJob(jobId, function (detail) {
      _trackFolderJobEvent(folder.path, detail);
    }, function (jobData) {
      _clearFolderProgress(folder.path);
      if (jobData.status === "done" && jobData.result) {
        var r = jobData.result;
        var parts = [];
        if (r.created > 0) parts.push("created " + r.created);
        if (r.updated > 0) parts.push("updated " + r.updated);
        if (r.skipped_final > 0) parts.push("skipped " + r.skipped_final);
        if (r.failed > 0) parts.push("failed " + r.failed);
        var summary = parts.length > 0 ? parts.join(", ") + "." : "Done.";
        toast("Markdown: " + summary);
      } else if (jobData.status === "failed") {
        var msg = "Markdown generation failed.";
        if (jobData.error) msg += " " + jobData.error;
        toast(msg);
      } else if (jobData.status === "cancelled") {
        toast("Markdown generation was cancelled.");
      } else if (jobData.status === "paused") {
        toast("Markdown generation paused. Completed files were saved.");
      }
      reload();
    });
  });
}

function _startFolderIndex(folder, mode, reload) {
  var method = mode === "retry" ? "retry_failed" : "start_index";
  var apiArgs = mode === "retry"
    ? [folder.path]
    : [
        folder.path,
        mode === "rebuild" ? "rebuild" : "incremental",
        null,
        _foldersState.outputMarkdownFiles,
      ];
  var label = mode === "retry" ? "Retrying " : mode === "rebuild" ? "Rebuilding " : "Indexing ";
  api(method, ...apiArgs).then(function (res) {
    if (!res.ok) {
      // A guard rejection (job already active, paused work waiting) must be
      // visible, not a silent no-op that looks like a broken button.
      toast(res.error ? res.error.message : "Could not start this run.");
      return;
    }
    var jobId = res.data.job_id;
    _setFolderProgress(folder.path, {
      phase: mode !== "retry" && _foldersState.outputMarkdownFiles ? "markdown" : "index",
      done: 0,
      total: null,
    });
    registerJobLabel(jobId, label + _folderBasename(folder.path));
    _watchFolderJob(jobId, function (detail) {
      _trackFolderJobEvent(folder.path, detail);
    }, function (jobData) {
      _clearFolderProgress(folder.path);
      if (jobData && jobData.kind === "failed") {
        toast("Indexing failed. " + (jobData.payload && jobData.payload.error ? jobData.payload.error : "Check Logs for details."));
      } else if (jobData && jobData.kind === "paused") {
        toast("Indexing paused. Completed files were saved.");
      }
      reload();
    });
  }).catch(function (err) {
    toast("Could not start indexing. Check that VectorDrive and Ollama are running.");
  });
}

function _addFolderFlow(onAdded) {
  var root = document.getElementById("dialog-root");
  if (root.children.length > 0) return;
  var state = { path: null, preview: null, scanning: false, error: null, warnings: null };

  var body = el("div", { class: "card", style: "max-width:520px" });

  function refresh() {
    body.innerHTML = "";
    body.appendChild(el("h2", { text: "Add folder" }));
    if (!state.path) {
      body.appendChild(el("p", { text: "Choose a folder to index." }));
    } else {
      body.appendChild(el("p", { text: truncateMiddle(state.path, 64), title: state.path, style: "font-weight:500" }));
      if (state.scanning) {
        body.appendChild(renderLoading({ label: "Scanning…" }));
      } else if (state.preview) {
        body.appendChild(el("p", { text: state.preview.supported_count + " supported · " + state.preview.unsupported_count + " unsupported · " + _fmtBytesF(state.preview.total_bytes) }));
        (state.warnings || []).forEach(function (w) { body.appendChild(el("p", { text: "⚠ " + w, style: "color:var(--color-warning)" })); });
      } else if (state.error) {
        body.appendChild(renderError({ message: state.error }));
      }
    }
    var actions = el("div", { style: "display:flex;gap:8px;justify-content:flex-end;margin-top:12px" });
    actions.appendChild(el("button", { class: "secondary", text: "Cancel", onclick: function () { overlay.remove(); } }));
    actions.appendChild(el("button", { text: state.path ? "Choose different…" : "Choose folder…", onclick: chooseAndScan }));
    if (state.path && state.preview) {
      actions.appendChild(el("button", { text: "Add", onclick: async function () {
        var res = await api("add_folder", state.path);
        if (res.ok) { overlay.remove(); onAdded(); }
        else { state.error = res.error ? res.error.message : "Could not add."; refresh(); }
      }}));
    }
    body.appendChild(actions);
  }

  async function chooseAndScan() {
    var chosen = await api("choose_folder");
    if (!chosen.ok || !chosen.data.path) return;
    state.path = chosen.data.path;
    state.preview = null; state.error = null; state.scanning = true;
    refresh();
    var validation = await api("validate_folder_path", state.path);
    if (validation.ok) state.warnings = validation.data.warnings;
    var previewJob = await api("preview_folder", state.path);
    if (!previewJob.ok) { state.scanning = false; state.error = previewJob.error.message; refresh(); return; }
    var jobId = previewJob.data.job_id;
    var poll = async function () {
      var job = await api("get_job", jobId);
      if (!job.ok) return;
      if (job.data.status === "done") { state.scanning = false; state.preview = job.data.result; refresh(); }
      else if (job.data.status === "failed") { state.scanning = false; state.error = job.data.error; refresh(); }
      else { setTimeout(poll, 300); }
    };
    poll();
  }

  var overlay = el("div", {
    role: "dialog", "aria-modal": "true",
    style: "position:absolute;top:0;right:0;bottom:0;left:0;pointer-events:auto;background:rgba(0,0,0,0.4);display:flex;align-items:center;justify-content:center",
  }, [body]);
  root.appendChild(overlay);
  refresh();
}

async function renderFolders(main) {
  var initial = await Promise.all([api("get_ui_state"), api("list_ocr_models")]);
  var uiRes = initial[0];
  var modelRes = initial[1];
  if (uiRes.ok && uiRes.data) {
    _foldersState.inspectorCollapsed = !!uiRes.data.folders_inspector_collapsed;
    _foldersState.outputMarkdownFiles = uiRes.data.output_markdown_files !== false;
  }
  if (modelRes.ok) {
    _foldersState.ocrModels = modelRes.data.models || [];
    _foldersState.selectedOcrModel = modelRes.data.selected_model;
    _foldersState.ocrModelError = null;
  } else {
    _foldersState.ocrModels = [];
    _foldersState.selectedOcrModel = null;
    _foldersState.ocrModelError =
      (modelRes.error && modelRes.error.message) || "Could not read local OCR models.";
  }
  var layout = el("div", { class: "content-with-inspector" });
  if (_foldersState.inspectorCollapsed) layout.classList.add("folders-inspector-collapsed");
  var contentArea = el("div", { class: "content-area" });
  var inspector = el("div", { class: "inspector" });
  layout.appendChild(contentArea);
  layout.appendChild(inspector);
  main.appendChild(layout);

  var inspectorCtx = {
    collapsed: _foldersState.inspectorCollapsed,
    setCollapsed: function (collapsed) {
      _foldersState.inspectorCollapsed = collapsed;
      inspectorCtx.collapsed = collapsed;
      layout.classList.toggle("folders-inspector-collapsed", collapsed);
      api("save_ui_state", { folders_inspector_collapsed: collapsed });
      var selected = _foldersState.folders.find(function (f) {
        return f.path === _foldersState.selected;
      });
      _renderInspector(inspector, selected || null, reload, inspectorCtx);
      if (!collapsed) {
        var heading = inspector.querySelector(".folder-inspector-heading");
        if (heading) heading.focus();
      }
    },
  };
  _foldersState.rerenderInspector = function () {
    var selected = _foldersState.folders.find(function (f) {
      return f.path === _foldersState.selected;
    });
    _renderInspector(inspector, selected || null, reload, inspectorCtx);
  };
  layout.addEventListener("keydown", function (e) {
    if (e.key === "Escape" && !inspectorCtx.collapsed) {
      inspectorCtx.setCollapsed(true);
    }
  });

  contentArea.appendChild(renderLoading({ label: "Loading folders…" }));
  _renderInspector(inspector, null, null, inspectorCtx);

  async function reload() {
    var results = await Promise.all([api("list_folders"), api("list_resumable_runs")]);
    var res = results[0];
    var runRes = results[1];
    if (!res.ok) {
      contentArea.innerHTML = "";
      contentArea.appendChild(renderError({ message: "Could not load folders.", hint: res.error && res.error.hint }));
      return;
    }
    _foldersState.folders = res.data;
    _renderFoldersTable(
      contentArea, inspector, res.data, runRes.ok ? runRes.data : [], reload, inspectorCtx
    );
  }

  await reload();
}

VIEW_RENDERERS.folders = renderFolders;
