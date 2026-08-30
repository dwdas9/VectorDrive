// Search view: mode selection, top-k, folder filter, file-type filter.
"use strict";

function _searchResultRow(r) {
  var filename = r.path.split("/").pop();
  var page = r.page_number ? "p." + r.page_number : "";
  var meta = [r.source_type, page, r.section].filter(Boolean).join(" · ");

  return el("div", { class: "search-result" }, [
    el("div", {}, [
      el("div", { style: "font-weight:600;font-size:13px" }, [
        el("span", { text: filename }),
        el("span", { text: " " }),
        el("span", { text: meta, style: "font-weight:400;font-size:11px;color:var(--color-text-muted)" }),
      ]),
      el("p", { class: "search-result-excerpt", text: r.excerpt }),
      el("p", { class: "mono-path", text: truncateMiddle(r.path, 72), title: r.path, style: "margin:2px 0 0" }),
      el("div", { style: "display:flex;gap:8px;margin-top:4px" }, [
        el("button", { class: "sm secondary", text: "Open", onclick: function () { api("open_document", r.path, r.page_number); } }),
        el("button", { class: "sm secondary", text: "Reveal", onclick: function () { api("reveal_document", r.path); } }),
      ]),
    ]),
    el("div", { class: "search-result-score", text: r.score.toFixed(4) }),
  ]);
}

function _getExtension(path) {
  var parts = path.split(".");
  return parts.length > 1 ? parts[parts.length - 1].toLowerCase() : "";
}

function renderSearch(main) {
  var state = { mode: "hybrid", topK: 10, hasSearched: false, query: "", folder: "", fileType: "" };

  var stage = el("div", { class: "search-stage" });
  main.appendChild(stage);

  var input = el("input", {
    type: "search",
    placeholder: "Search your indexed documents…",
    "aria-label": "Search query",
    style: "width:100%;font-size:16px;padding:14px 16px",
  });

  var modeControl = segmentedControl(
    [
      { value: "hybrid", label: "Hybrid", title: "Keyword + semantic, fused" },
      { value: "fts", label: "Keyword", title: "Full-text keyword search (FTS5)" },
      { value: "vector", label: "Semantic", title: "Semantic similarity (vector)" },
    ],
    state.mode,
    function (value) { state.mode = value; if (state.hasSearched) runSearch(); }
  );

  var topKSelect = el("select", { "aria-label": "Results count" }, [
    el("option", { value: "5", text: "Top 5" }),
    el("option", { value: "10", text: "Top 10", selected: "selected" }),
    el("option", { value: "20", text: "Top 20" }),
    el("option", { value: "50", text: "Top 50" }),
  ]);
  topKSelect.value = "10";
  topKSelect.addEventListener("change", function () {
    state.topK = parseInt(topKSelect.value, 10);
    if (state.hasSearched) runSearch();
  });

  var folderSelect = el("select", { "aria-label": "Filter by folder" }, [
    el("option", { value: "", text: "All folders" }),
  ]);
  folderSelect.addEventListener("change", function () {
    state.folder = folderSelect.value;
    if (state.hasSearched) runSearch();
  });

  var typeSelect = el("select", { "aria-label": "Filter by file type" }, [
    el("option", { value: "", text: "All types" }),
    el("option", { value: "pdf", text: "PDF" }),
    el("option", { value: "docx", text: "DOCX" }),
    el("option", { value: "txt", text: "Text" }),
    el("option", { value: "md", text: "Markdown" }),
    el("option", { value: "png", text: "PNG" }),
    el("option", { value: "jpg", text: "JPG" }),
  ]);
  typeSelect.addEventListener("change", function () {
    state.fileType = typeSelect.value;
    if (state.hasSearched) runSearch();
  });

  var searchBtn = el("button", { text: "Search" });

  var controlsRow = el("div", { class: "search-controls", style: "display:flex;gap:12px;align-items:center;justify-content:center;margin-top:12px;flex-wrap:wrap" }, [
    modeControl,
    topKSelect,
    folderSelect,
    typeSelect,
    searchBtn,
  ]);

  var bar = el("div", { class: "search-bar" }, [input, controlsRow]);
  stage.appendChild(bar);

  var resultsArea = el("div", { class: "search-results" });
  stage.appendChild(resultsArea);

  function layoutForState() {
    stage.classList.toggle("search-stage-centered", !state.hasSearched);
    stage.classList.toggle("search-stage-top", state.hasSearched);
  }
  layoutForState();

  async function runSearch() {
    var query = input.value.trim();
    if (!query) return;
    state.hasSearched = true;
    state.query = query;
    layoutForState();
    resultsArea.innerHTML = "";
    resultsArea.appendChild(renderLoading({ label: "Searching…" }));

    var res = await api("search", query, state.mode, state.topK);
    resultsArea.innerHTML = "";
    if (!res.ok) {
      var isOllamaIssue = res.error && res.error.code === "embedding_failed";
      resultsArea.appendChild(renderError({
        message: isOllamaIssue ? "Vector/hybrid search needs Ollama running. FTS still works." : "Search failed.",
        hint: res.error && res.error.hint,
        actionLabel: isOllamaIssue ? "Switch to Keyword" : undefined,
        onAction: isOllamaIssue ? function () {
          var ftsBtn = [].slice.call(modeControl.querySelectorAll("button")).find(function (b) { return b.textContent === "Keyword"; });
          if (ftsBtn) ftsBtn.click();
        } : undefined,
      }));
      return;
    }

    var results = res.data.results;

    if (state.folder) {
      results = results.filter(function (r) { return r.path.startsWith(state.folder); });
    }

    if (state.fileType) {
      results = results.filter(function (r) { return _getExtension(r.path) === state.fileType; });
    }

    if (results.length === 0) {
      resultsArea.appendChild(renderEmpty({ title: "No results.", detail: "Try a broader query, switch modes, or adjust filters." }));
      return;
    }
    resultsArea.appendChild(el("p", {
      text: results.length + " results · " + state.mode + " · top " + state.topK,
      style: "color:var(--color-text-muted);margin-bottom:8px;font-size:12px",
    }));
    results.forEach(function (r) { resultsArea.appendChild(_searchResultRow(r)); });
  }

  searchBtn.addEventListener("click", runSearch);
  input.addEventListener("keydown", function (e) { if (e.key === "Enter") runSearch(); });
  input.focus();

  // Populate folder filter from API
  api("list_folders").then(function (res) {
    if (res.ok && res.data.length > 0) {
      res.data.forEach(function (f) {
        var name = f.path.replace(/\/$/, "").split("/").pop();
        folderSelect.appendChild(el("option", { value: f.path, text: name }));
      });
    }
  });
}

VIEW_RENDERERS.search = renderSearch;
