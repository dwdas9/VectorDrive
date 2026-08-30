// Settings / Advanced screen (v0.2 G9): data home info, logs, integrity
// check, backup/restore, vacuum, WAL checkpoint.
"use strict";

function _fmtSize(bytes) {
  if (!bytes) return "0 B";
  if (bytes < 1024) return bytes + " B";
  var units = ["KB", "MB", "GB"];
  var v = bytes / 1024, i = 0;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  return v.toFixed(1) + " " + units[i];
}

async function renderSettings(main) {
  main.appendChild(el("h1", { text: "Settings" }));
  var body = el("div", {});
  main.appendChild(body);
  body.appendChild(renderLoading({ label: "Loading settings…" }));

  var infoRes = await api("settings_info");
  body.innerHTML = "";
  if (!infoRes.ok) {
    body.appendChild(renderError({ message: "Could not load settings info." }));
    return;
  }
  var info = infoRes.data;

  var infoCard = el("div", { class: "card" }, [
    el("h2", { text: "Data location" }),
    el("p", { text: info.data_home, style: "font-size:13px;color:var(--color-text-muted);word-break:break-all" }),
    el("p", { text: "Database: " + (info.db_exists ? _fmtSize(info.db_size_bytes) : "not created yet"), style: "color:var(--color-text-muted)" }),
    el("p", { text: "Log: " + (info.log_exists ? _fmtSize(info.log_size_bytes) : "no log yet"), style: "color:var(--color-text-muted)" }),
  ]);
  body.appendChild(infoCard);

  // Integrity check
  var integritySection = el("div", { class: "card", style: "margin-top:16px" });
  body.appendChild(integritySection);
  integritySection.appendChild(el("h2", { text: "Database integrity" }));
  var integrityResult = el("div", {});
  integritySection.appendChild(integrityResult);
  var integrityBtn = el("button", { text: "Run integrity check" });
  integritySection.appendChild(integrityBtn);

  integrityBtn.addEventListener("click", async function () {
    integrityResult.innerHTML = "";
    integrityResult.appendChild(renderLoading({ label: "Checking…" }));
    integrityBtn.disabled = true;
    var res = await api("settings_integrity_check");
    integrityResult.innerHTML = "";
    integrityBtn.disabled = false;
    if (res.ok) {
      var d = res.data;
      integrityResult.appendChild(el("p", {}, [
        statusPill(d.ok ? "pass" : "fail", d.ok ? "OK" : "Problem"),
        document.createTextNode(" " + d.message),
      ]));
    } else {
      integrityResult.appendChild(renderError({ message: "Check failed." }));
    }
  });

  // Backup section
  var backupSection = el("div", { class: "card", style: "margin-top:16px" });
  body.appendChild(backupSection);
  backupSection.appendChild(el("h2", { text: "Database backup" }));

  var backupBtn = el("button", { text: "Back up now" });
  var backupStatus = el("div", {});
  backupSection.appendChild(el("div", { style: "display:flex;gap:12px;align-items:center" }, [backupBtn, backupStatus]));

  backupBtn.addEventListener("click", async function () {
    backupStatus.innerHTML = "";
    backupBtn.disabled = true;
    var res = await api("settings_backup_db");
    backupBtn.disabled = false;
    if (res.ok) {
      _watchJob(res.data.job_id, function () {}, function (evt) {
        if (evt.kind === "done" && evt.payload && evt.payload.result) {
          backupStatus.innerHTML = "";
          backupStatus.appendChild(el("span", { text: "Backed up (" + _fmtSize(evt.payload.result.size_bytes) + ")", style: "color:var(--color-text-muted)" }));
          refreshBackupList();
        } else {
          backupStatus.innerHTML = "";
          backupStatus.appendChild(el("span", { text: "Backup failed.", style: "color:var(--color-danger)" }));
        }
      });
    } else {
      backupStatus.appendChild(el("span", { text: res.error ? res.error.message : "Backup failed.", style: "color:var(--color-danger)" }));
    }
  });

  var backupListEl = el("div", { style: "margin-top:8px" });
  backupSection.appendChild(backupListEl);

  async function refreshBackupList() {
    var listRes = await api("settings_list_backups");
    backupListEl.innerHTML = "";
    if (listRes.ok && listRes.data.length > 0) {
      listRes.data.forEach(function (b) {
        backupListEl.appendChild(el("p", { text: b.name + " · " + _fmtSize(b.size_bytes), style: "font-size:13px;color:var(--color-text-muted);margin:2px 0" }));
      });
    } else {
      backupListEl.appendChild(el("p", { text: "No backups yet.", style: "color:var(--color-text-muted)" }));
    }
  }
  refreshBackupList();

  // Maintenance section
  var maintSection = el("div", { class: "card", style: "margin-top:16px" });
  body.appendChild(maintSection);
  maintSection.appendChild(el("h2", { text: "Maintenance" }));
  maintSection.appendChild(el("p", { text: "These actions are safe and do not modify your source documents.", style: "color:var(--color-text-muted);margin-bottom:8px" }));

  var vacuumBtn = el("button", { class: "secondary", text: "Compact database (VACUUM)" });
  var walBtn = el("button", { class: "secondary", text: "Checkpoint WAL" });
  var maintStatus = el("div", { style: "margin-top:8px" });
  maintSection.appendChild(el("div", { style: "display:flex;gap:8px" }, [vacuumBtn, walBtn]));
  maintSection.appendChild(maintStatus);

  vacuumBtn.addEventListener("click", function () {
    confirmDialog({
      title: "Compact database",
      body: "This rewrites the database to reclaim unused space. It may take a moment for large indexes. No data is lost.",
      confirmLabel: "Compact",
      danger: false,
      onConfirm: async function () {
        maintStatus.innerHTML = "";
        maintStatus.appendChild(renderLoading({ label: "Compacting…" }));
        vacuumBtn.disabled = true;
        var res = await api("settings_vacuum");
        if (res.ok) {
          _watchJob(res.data.job_id, function () {}, function (evt) {
            maintStatus.innerHTML = "";
            vacuumBtn.disabled = false;
            if (evt.kind === "done" && evt.payload && evt.payload.result) {
              var r = evt.payload.result;
              maintStatus.appendChild(el("p", { text: "Reclaimed " + _fmtSize(r.reclaimed) + " (" + _fmtSize(r.size_before) + " → " + _fmtSize(r.size_after) + ")" }));
            } else {
              maintStatus.appendChild(el("p", { text: "Vacuum failed.", style: "color:var(--color-danger)" }));
            }
          });
        } else {
          maintStatus.innerHTML = "";
          vacuumBtn.disabled = false;
          maintStatus.appendChild(el("p", { text: res.error ? res.error.message : "Failed.", style: "color:var(--color-danger)" }));
        }
      },
    });
  });

  walBtn.addEventListener("click", async function () {
    var res = await api("settings_wal_checkpoint");
    maintStatus.innerHTML = "";
    if (res.ok) {
      maintStatus.appendChild(el("p", { text: "Checkpointed " + res.data.checkpointed + " frames.", style: "color:var(--color-text-muted)" }));
    } else {
      maintStatus.appendChild(el("p", { text: res.error ? res.error.message : "Checkpoint failed.", style: "color:var(--color-danger)" }));
    }
  });

  // Danger zone — clearly separated from the safe Maintenance actions above.
  var dangerSection = el("div", { class: "card", style: "margin-top:16px;border-color:var(--color-danger)" });
  body.appendChild(dangerSection);
  dangerSection.appendChild(el("h2", { text: "Danger zone" }));
  dangerSection.appendChild(el("p", {
    text: "Compact database (above) is the safe option — it only reclaims unused space. The action below erases indexed data.",
    style: "color:var(--color-text-muted);margin-bottom:8px",
  }));

  var resetBtn = el("button", { class: "danger", text: "Reset VectorDrive data…" });
  var resetStatus = el("div", { style: "margin-top:8px" });
  dangerSection.appendChild(resetBtn);
  dangerSection.appendChild(resetStatus);

  var RESET_CONFIRM_BODY =
    "This permanently removes VectorDrive's database and indexes, OCR and embedding caches, " +
    "processing history, logs, backups, and review handoffs, then creates a fresh empty database. " +
    "Your original source documents, adjacent Markdown sidecars, settings, and registered folders remain.";

  resetBtn.addEventListener("click", function () {
    confirmDialog({
      title: "Reset VectorDrive data",
      body: RESET_CONFIRM_BODY,
      confirmLabel: "Reset data",
      danger: true,
      onConfirm: async function () {
        resetStatus.innerHTML = "";
        resetStatus.appendChild(renderLoading({ label: "Resetting…" }));
        resetBtn.disabled = true;
        var res = await api("settings_reset_data");
        if (!res.ok) {
          resetStatus.innerHTML = "";
          resetBtn.disabled = false;
          resetStatus.appendChild(el("p", {
            text: res.error ? res.error.message : "Reset failed.",
            style: "color:var(--color-danger)",
          }));
          return;
        }
        registerJobLabel(res.data.job_id, "Resetting VectorDrive data");
        _watchJob(res.data.job_id, function () {}, function (evt) {
          resetStatus.innerHTML = "";
          resetBtn.disabled = false;
          if (evt.kind === "done") {
            toast("VectorDrive data reset — a fresh empty database was created.");
            navigate("overview");
          } else {
            resetStatus.appendChild(el("p", {
              text: (evt.payload && evt.payload.error) ? evt.payload.error : "Reset failed.",
              style: "color:var(--color-danger)",
            }));
          }
        });
      },
    });
  });

  // Logs section
  var logSection = el("div", { class: "card", style: "margin-top:16px" });
  body.appendChild(logSection);
  logSection.appendChild(el("h2", { text: "Logs" }));
  var logContent = el("div", {});
  logSection.appendChild(logContent);

  var logBtn = el("button", { class: "secondary", text: "Show recent logs" });
  logSection.appendChild(logBtn);

  logBtn.addEventListener("click", async function () {
    logContent.innerHTML = "";
    logContent.appendChild(renderLoading({ label: "Loading logs…" }));
    var res = await api("settings_logs", 100);
    logContent.innerHTML = "";
    if (res.ok) {
      if (res.data.lines.length === 0) {
        logContent.appendChild(el("p", { text: "No log entries yet.", style: "color:var(--color-text-muted)" }));
      } else {
        var pre = el("pre", { style: "white-space:pre-wrap;font-size:11px;max-height:300px;overflow-y:auto;background:var(--color-surface);padding:8px;border-radius:4px;border:1px solid var(--color-border)" });
        pre.textContent = res.data.lines.join("\n");
        logContent.appendChild(pre);
        logContent.appendChild(el("p", { text: "Log file: " + _fmtSize(res.data.total_bytes), style: "font-size:12px;color:var(--color-text-muted)" }));
      }
    } else {
      logContent.appendChild(renderError({ message: "Could not load logs." }));
    }
  });
}

VIEW_RENDERERS.settings = renderSettings;
