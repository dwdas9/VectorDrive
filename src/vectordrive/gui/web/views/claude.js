// Claude Desktop integration screen (v0.2 G8): detect, test, connect,
// disconnect, rollback — with clear tri-state display.
"use strict";

function _stateLabel(connectionState) {
  var map = {
    not_installed: { label: "Not installed", pill: "fail" },
    not_configured: { label: "Not configured", pill: "warn" },
    configured_untested: { label: "Configured (untested)", pill: "warn" },
    configured_stale: { label: "Configured (stale)", pill: "warn" },
    configured_failing: { label: "Configured (failing)", pill: "fail" },
    verified: { label: "Verified & working", pill: "pass" },
  };
  return map[connectionState] || { label: connectionState, pill: "warn" };
}

function _diagnosticsPanel(status, quickResult) {
  var sl = _stateLabel(status.connection_state);
  var rows = [
    el("h2", { text: "System diagnostics" }),
    el("div", { style: "display:flex;justify-content:space-between;align-items:center" }, [
      el("span", { text: "Connection status" }),
      statusPill(sl.pill, sl.label),
    ]),
  ];
  if (status.app_installed) {
    rows.push(el("p", { text: "Claude Desktop: installed" + (status.app_running ? " (running)" : ""), style: "color:var(--color-text-muted);margin:4px 0 0" }));
  }
  if (status.entry_present && status.other_server_names.length > 0) {
    rows.push(el("p", { text: "Other MCP servers: " + status.other_server_names.join(", "), style: "color:var(--color-text-muted);margin:4px 0 0" }));
  }
  if (status.restart_required && status.entry_present) {
    rows.push(el("p", { text: "Restart Claude Desktop to pick up config changes.", style: "color:var(--color-warning);margin:8px 0 0" }));
  }

  rows.push(el("div", { style: "border-top:1px solid var(--color-border);margin:12px 0" }));
  rows.push(el("div", { style: "display:flex;justify-content:space-between;align-items:center" }, [
    el("span", { text: "MCP server binary" }),
    quickResult ? statusPill(quickResult.ok ? "pass" : "fail", quickResult.ok ? "Found" : "Missing") : el("span", { class: "skeleton", style: "width:60px;display:inline-block" }),
  ]));
  if (quickResult) {
    rows.push(el("p", { text: quickResult.message, style: "color:var(--color-text-muted);margin:4px 0 0" }));
  }

  return el("div", { class: "card" }, rows);
}

async function renderClaude(main) {
  main.appendChild(el("h1", { text: "Claude Desktop" }));
  var body = el("div", {});
  main.appendChild(body);
  body.appendChild(renderLoading({ label: "Detecting Claude Desktop…" }));

  var state = { status: null, quickResult: null };

  async function reload() {
    var res = await api("claude_detect");
    body.innerHTML = "";
    if (!res.ok) {
      body.appendChild(renderError({ message: "Could not detect Claude Desktop.", hint: res.error && res.error.hint }));
      return;
    }
    state.status = res.data;

    if (state.status.connection_state === "not_installed") {
      body.appendChild(_diagnosticsPanel(state.status, null));
      body.appendChild(renderEmpty({ title: "Claude Desktop is not installed.", detail: "Install Claude Desktop from claude.ai, then return here." }));
      return;
    }

    var diagPanel = _diagnosticsPanel(state.status, null);
    body.appendChild(diagPanel);

    var quickRes = await api("claude_quick_test");
    if (quickRes.ok) {
      state.quickResult = quickRes.data;
      diagPanel.replaceWith(_diagnosticsPanel(state.status, state.quickResult));
    }

    // Deep test: the primary (accent) action on this screen.
    var deepSection = el("div", { style: "margin-top:16px" });
    body.appendChild(deepSection);
    var deepBtn = el("button", { text: "Run full MCP test" });
    var deepResult = el("div", {});
    deepSection.appendChild(el("div", { style: "display:flex;gap:12px;align-items:center" }, [deepBtn, deepResult]));

    deepBtn.addEventListener("click", async function () {
      deepResult.innerHTML = "";
      deepResult.appendChild(renderLoading({ label: "Testing MCP server…" }));
      deepBtn.disabled = true;
      var r = await api("claude_deep_test");
      if (!r.ok) {
        deepResult.innerHTML = "";
        deepResult.appendChild(renderError({ message: "Could not start deep test." }));
        deepBtn.disabled = false;
        return;
      }
      _watchJob(r.data.job_id, function () {}, async function (evt) {
        deepResult.innerHTML = "";
        deepBtn.disabled = false;
        if (evt.kind === "done" && evt.payload && evt.payload.result) {
          var dr = evt.payload.result;
          if (dr.ok) {
            deepResult.appendChild(el("span", {}, [
              statusPill("pass", "Working"),
              document.createTextNode(" " + dr.tools.length + " tools · " + dr.latency_s + "s"),
            ]));
          } else {
            deepResult.appendChild(el("span", {}, [
              statusPill("fail", "Failed"),
              document.createTextNode(" " + (dr.error || "Unknown error")),
            ]));
          }
        } else {
          deepResult.appendChild(statusPill("fail", "Test failed"));
        }
        var fresh = await api("claude_detect");
        if (fresh.ok) {
          state.status = fresh.data;
          var oldPanel = body.querySelector(".card");
          if (oldPanel) oldPanel.replaceWith(_diagnosticsPanel(state.status, state.quickResult));
        }
      });
    });

    // Connect / disconnect actions — "Update config" is a secondary
    // (outline) action next to the deep test's primary accent button;
    // "Disconnect" is a plain text link (see below), never competing
    // visually with either.
    var actions = el("div", { style: "margin-top:24px" });
    body.appendChild(actions);

    if (!state.status.entry_present) {
      actions.appendChild(el("h2", { text: "Connect VectorDrive" }));
      actions.appendChild(el("p", { text: "Add the VectorDrive MCP server to Claude Desktop’s configuration." }));
      var connectBtn = el("button", { text: "Preview connection…" });
      actions.appendChild(connectBtn);

      connectBtn.addEventListener("click", async function () {
        var prev = await api("claude_preview_connect");
        if (!prev.ok) {
          toast(prev.error ? prev.error.message : "Preview failed.");
          return;
        }
        _showClaudeDiffDialog("Connect VectorDrive", prev.data, reload);
      });
    } else {
      actions.appendChild(el("h2", { text: "Manage connection" }));
      var actionRow = el("div", { style: "display:flex;gap:8px;align-items:center" });
      actions.appendChild(actionRow);

      var reconnectBtn = el("button", { class: "secondary", text: "Update config" });
      reconnectBtn.addEventListener("click", async function () {
        var prev = await api("claude_preview_connect");
        if (!prev.ok) { toast(prev.error ? prev.error.message : "Preview failed."); return; }
        if (prev.data.is_noop) { toast("Configuration is already up to date."); return; }
        _showClaudeDiffDialog("Update VectorDrive config", prev.data, reload);
      });
      actionRow.appendChild(reconnectBtn);

      var disconnectBtn = el("button", { class: "link", text: "Disconnect" });
      disconnectBtn.addEventListener("click", async function () {
        var prev = await api("claude_preview_disconnect");
        if (!prev.ok) { toast(prev.error ? prev.error.message : "Preview failed."); return; }
        if (prev.data.is_noop) { toast("VectorDrive is not currently configured."); return; }
        _showClaudeDiffDialog("Disconnect VectorDrive", prev.data, reload);
      });
      actionRow.appendChild(disconnectBtn);
    }

    // Backups section — restoring a backup is also a plain text link.
    var backupsSection = el("div", { style: "margin-top:24px" });
    body.appendChild(backupsSection);
    backupsSection.appendChild(el("h2", { text: "Backups" }));
    var backupListEl = el("div", {});
    backupsSection.appendChild(backupListEl);

    var bRes = await api("claude_list_backups");
    if (bRes.ok && bRes.data.length > 0) {
      bRes.data.forEach(function (bp) {
        var fname = bp.split("/").pop();
        var bRow = el("div", { style: "display:flex;gap:8px;align-items:center;margin-bottom:4px" }, [
          el("span", { text: fname, style: "font-size:13px;color:var(--color-text-muted)" }),
          el("button", {
            class: "link", text: "Restore", style: "font-size:12px",
            onclick: function () {
              confirmDialog({
                title: "Restore backup",
                body: "Restore Claude Desktop config from " + fname + "? A backup of the current config will be created first.",
                confirmLabel: "Restore",
                danger: false,
                onConfirm: async function () {
                  var rr = await api("claude_restore_backup", bp);
                  if (rr.ok) { toast("Config restored."); reload(); }
                  else toast(rr.error ? rr.error.message : "Restore failed.");
                },
              });
            },
          }),
        ]);
        backupListEl.appendChild(bRow);
      });
    } else {
      backupListEl.appendChild(el("p", { text: "No backups yet.", style: "color:var(--color-text-muted)" }));
    }
  }

  await reload();
}

function _showClaudeDiffDialog(title, diffData, onApplied) {
  var root = document.getElementById("dialog-root");
  // G12 finding: guard against a duplicate overlay stacking on a
  // repeat click; see components.js's confirmDialog for the same fix.
  if (root.children.length > 0) return;
  var overlay = el(
    "div",
    // position:absolute, not fixed — #dialog-root (styles.css) is
    // already the fixed, viewport-anchored container; explicit
    // top/right/bottom/left, not the unsupported `inset` shorthand.
    { role: "alertdialog", "aria-modal": "true", "aria-labelledby": "dlg-title",
      style: "position:absolute;top:0;right:0;bottom:0;left:0;pointer-events:auto;background:rgba(0,0,0,0.4);display:flex;align-items:center;justify-content:center" },
    [el("div", { class: "card", style: "max-width:620px;max-height:80vh;overflow-y:auto" }, [
      el("h2", { id: "dlg-title", text: title }),
      el("p", { text: diffData.preserved_server_count + " other server(s) preserved.", style: "color:var(--color-text-muted)" }),
      diffView(diffData.unified_diff || "(no changes)"),
      el("div", { style: "display:flex;gap:8px;justify-content:flex-end;margin-top:12px" }, [
        el("button", { class: "secondary", text: "Cancel", onclick: function () { overlay.remove(); } }),
        el("button", {
          text: "Apply",
          onclick: async function () {
            var res = await api("claude_apply", diffData);
            overlay.remove();
            if (res.ok) { toast("Config updated."); onApplied(); }
            else toast(res.error ? res.error.message : "Apply failed.");
          },
        }),
      ]),
    ])]
  );
  root.appendChild(overlay);
  overlay.querySelector("button").focus();
}

VIEW_RENDERERS.claude = renderClaude;
