// Overview: health checks, index stats, embedding/model info, next action.
"use strict";

function _fmtBytesO(n) {
  if (!n && n !== 0) return "—";
  if (n < 1024) return n + " B";
  var units = ["KB", "MB", "GB"];
  var v = n / 1024, i = 0;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  return v.toFixed(1) + " " + units[i];
}

function _checkStatus(check) {
  return check.status === "PASS" ? "pass" : check.status === "WARN" ? "warn" : "fail";
}

function _renderOverviewContent(main, data) {
  var healthCard = el("div", { class: "card" }, [el("h2", { text: "Health Checks" })]);
  var grid = el("div", { class: "chip-grid" });
  data.checks.forEach(function (c) {
    grid.appendChild(statusChip(_checkStatus(c), c.name, c.message));
  });
  if (!data.db_available) {
    grid.appendChild(statusChip("fail", "database", "Database appears missing or corrupt."));
  }
  healthCard.appendChild(grid);
  main.appendChild(healthCard);

  if (data.status) {
    var s = data.status;
    var counts = s.file_status_counts || {};
    var indexed = counts.indexed || 0;
    var failed = counts.failed || 0;

    var statsCard = el("div", { class: "card" }, [
      el("h2", { text: "Index Statistics" }),
      el("div", { style: "display:grid;grid-template-columns:repeat(auto-fill,minmax(140px,1fr));gap:12px" }, [
        statCallout(indexed, "files indexed"),
        statCallout(failed, "failed"),
        statCallout(s.chunk_count || 0, "chunks"),
        statCallout(s.active_embedding_count || 0, "embeddings"),
      ]),
      el("p", {
        style: "margin-top:8px;font-size:12px;color:var(--color-text-muted)",
        text:
          (s.cached_embedding_count || 0).toLocaleString() +
          " embedding vector(s) held in the reusable cache — content-addressed, kept for reuse across re-indexing, not part of the indexed corpus.",
      }),
      el("button", {
        class: "secondary",
        text: "Manage storage…",
        style: "margin-top:8px",
        onclick: function () { navigate("settings"); },
      }),
    ]);
    main.appendChild(statsCard);

    var modelCard = el("div", { class: "card" }, [
      el("h2", { text: "Configuration" }),
      el("ul", { class: "stat-list" }, [
        el("li", {}, [el("span", { class: "stat-label", text: "Embedding provider" }), el("span", { class: "stat-value", text: s.embedding_provider || "—" })]),
        el("li", {}, [el("span", { class: "stat-label", text: "Embedding model" }), el("span", { class: "stat-value", text: s.embedding_model || "—" })]),
        el("li", {}, [el("span", { class: "stat-label", text: "Dimensions" }), el("span", { class: "stat-value", text: String(s.embedding_dims || "—") })]),
        el("li", {}, [el("span", { class: "stat-label", text: "Vector search engine" }), el("span", { class: "stat-value", text: s.vector_backend || "—" })]),
        el("li", {}, [el("span", { class: "stat-label", text: "Database file" }), el("span", { class: "stat-value", text: _fmtBytesO(s.db_file_bytes) })]),
        el("li", {}, [el("span", { class: "stat-label", text: "Reusable free space" }), el("span", { class: "stat-value", text: _fmtBytesO(s.db_free_bytes) })]),
        el("li", {}, [el("span", { class: "stat-label", text: "WAL size" }), el("span", { class: "stat-value", text: _fmtBytesO(s.wal_bytes) })]),
        el("li", {}, [el("span", { class: "stat-label", text: "OCR warnings" }), el("span", { class: "stat-value", text: String(s.ocr_warning_count || 0) })]),
      ]),
      el("p", {
        style: "margin-top:8px;font-size:12px;color:var(--color-text-muted)",
        text:
          "\"Vector search engine\" is the configured search infrastructure (e.g. sqlite_vec) — it does not indicate that any folder currently has vectors. " +
          "The embeddings statistic above is the true count of vectors backing the current corpus.",
      }),
    ]);
    if (s.latest_run) {
      modelCard.appendChild(el("p", { text: "Last run: " + s.latest_run.started_at, style: "margin-top:8px;font-size:12px;color:var(--color-text-muted)" }));
    }
    main.appendChild(modelCard);
  }

  if (data.next_action) {
    var na = data.next_action;
    var actionCard = el("div", { class: "card" }, [
      el("h2", { text: "Recommended Next Step" }),
      el("p", { text: na.message }),
      na.route
        ? el("button", { text: na.button_label, onclick: function () { navigate(na.route); } })
        : el("button", { class: "secondary", text: na.button_label, onclick: function () { render(); } }),
    ]);
    main.appendChild(actionCard);
  }
}

async function renderOverview(main) {
  var header = el("div", { class: "page-header" }, [
    el("h1", { text: "Overview" }),
    el("div", { class: "page-header-actions" }, [
      el("button", { class: "secondary", text: "Refresh", onclick: function () { render(); } }),
    ]),
  ]);
  main.appendChild(header);

  var body = el("div", {});
  main.appendChild(body);
  body.appendChild(renderLoading({ label: "Checking system health…" }));

  var res = await api("get_overview");
  body.innerHTML = "";
  if (!res.ok) {
    body.appendChild(renderError({
      message: "Could not load overview.",
      hint: res.error && res.error.hint,
      actionLabel: "Retry",
      onAction: function () { render(); },
    }));
    return;
  }
  _renderOverviewContent(body, res.data);
}

VIEW_RENDERERS.overview = renderOverview;
