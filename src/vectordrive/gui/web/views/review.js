// Review view: OCR quality manual-review queue (flagged pages), a right-
// side inspector with the page's extracted text/metadata, and per-file
// actions. This screen never uploads anything or contacts a remote model
// itself -- "Prepare for ChatGPT"/"Prepare for Claude" only write a local
// manifest, review prompt, and a rendered/copied page image under
// VectorDrive's own data directory, which the user opens/pastes/attaches
// themselves. "Remove from VectorDrive" never deletes or modifies the
// original source file -- only VectorDrive's own derived data and the
// generated *.vectordrive.md sidecar.
"use strict";

var _reviewState = {
  selected: null,
  selectedEntry: null,
  entries: [],
  detail: null,
  detailLoading: false,
  detailError: null,
  inspectorCollapsed: false,
};

function _decisionPill(decision) {
  if (decision === "extraction_failed") return statusPill("fail", "Extraction failed");
  if (decision === "manual_review_recommended") return statusPill("warn", "Needs review");
  return statusPill("pass", decision);
}

function _reasonsSummary(reasons) {
  if (!reasons || reasons.length === 0) return "(no reasons recorded)";
  return reasons.map(function (r) { return r.code; }).join(", ");
}

async function _copyToClipboard(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch (err) {
    return false;
  }
}

async function renderReview(main) {
  var uiRes = await api("get_ui_state");
  if (uiRes.ok && uiRes.data) {
    _reviewState.inspectorCollapsed = !!uiRes.data.review_inspector_collapsed;
  }

  main.appendChild(el("div", { class: "page-header" }, [el("h1", { text: "Review queue" })]));
  main.appendChild(
    el("p", {
      text:
        "Pages flagged during indexing for manual review or that failed OCR entirely. " +
        "Nothing here is ever uploaded automatically. Select a page to see its extracted " +
        "text on the right. \"Remove from VectorDrive\" excludes a file from future indexing " +
        "and deletes VectorDrive's derived data for it -- your original file is never touched.",
      style: "color:var(--color-text-muted);max-width:660px",
    })
  );

  var layout = el("div", { class: "content-with-inspector" });
  if (_reviewState.inspectorCollapsed) layout.classList.add("review-inspector-collapsed");
  var contentArea = el("div", { class: "content-area" });
  var inspector = el("div", { class: "inspector", id: "review-inspector" });
  layout.appendChild(contentArea);
  layout.appendChild(inspector);
  main.appendChild(layout);

  var body = el("div", {});
  contentArea.appendChild(body);
  body.appendChild(renderLoading({ label: "Loading review queue…" }));

  function persistCollapsed() {
    api("save_ui_state", { review_inspector_collapsed: _reviewState.inspectorCollapsed });
  }

  function setCollapsed(collapsed) {
    _reviewState.inspectorCollapsed = collapsed;
    layout.classList.toggle("review-inspector-collapsed", collapsed);
    persistCollapsed();
    renderInspector();
  }

  layout.addEventListener("keydown", function (e) {
    if (e.key === "Escape" && !_reviewState.inspectorCollapsed) {
      setCollapsed(true);
    }
  });

  async function selectEntry(entry) {
    _reviewState.selected = entry.page_review_id;
    _reviewState.selectedEntry = entry;
    _reviewState.detail = null;
    _reviewState.detailError = null;
    _reviewState.detailLoading = true;
    if (_reviewState.inspectorCollapsed) setCollapsed(false);
    renderList();
    renderInspector();

    var res = await api("get_review_detail", entry.page_review_id);
    if (_reviewState.selected !== entry.page_review_id) return; // superseded
    _reviewState.detailLoading = false;
    if (res.ok) {
      _reviewState.detail = res.data;
    } else {
      _reviewState.detailError =
        (res.error && res.error.message) || "Could not load this page's detail.";
    }
    renderInspector();
    var heading = inspector.querySelector(".review-inspector-heading");
    if (heading) heading.focus();
  }

  function renderInspector() {
    inspector.innerHTML = "";

    var toggle = el("button", {
      class: "review-inspector-toggle",
      type: "button",
      title: _reviewState.inspectorCollapsed ? "Expand inspector" : "Collapse inspector",
      "aria-label": _reviewState.inspectorCollapsed
        ? "Expand page inspector"
        : "Collapse page inspector",
      "aria-expanded": _reviewState.inspectorCollapsed ? "false" : "true",
      text: _reviewState.inspectorCollapsed ? "‹" : "›",
      onclick: function () { setCollapsed(!_reviewState.inspectorCollapsed); },
    });

    if (_reviewState.inspectorCollapsed) {
      inspector.appendChild(toggle);
      return;
    }

    var heading = el("h2", {
      class: "review-inspector-heading",
      tabindex: "-1",
      text: _reviewState.selectedEntry
        ? "Page " + _reviewState.selectedEntry.page_number
        : "Page detail",
    });
    var closeBtn = el("button", {
      class: "review-inspector-close secondary sm",
      type: "button",
      text: "Close",
      "aria-label": "Clear selected page",
      onclick: function () {
        _reviewState.selected = null;
        _reviewState.selectedEntry = null;
        _reviewState.detail = null;
        renderList();
        renderInspector();
      },
    });
    inspector.appendChild(
      el("div", { class: "review-inspector-header" }, [
        heading,
        el("div", { style: "display:flex;gap:var(--space-2)" }, [closeBtn, toggle]),
      ])
    );

    var bodyEl = el("div", { class: "inspector-body" });
    inspector.appendChild(bodyEl);

    if (!_reviewState.selectedEntry) {
      bodyEl.appendChild(
        renderEmpty({
          title: "No page selected",
          detail: "Select a flagged page on the left to see its extracted text and warnings.",
        })
      );
      return;
    }
    if (_reviewState.detailLoading) {
      bodyEl.appendChild(renderLoading({ label: "Loading page detail…" }));
      return;
    }
    if (_reviewState.detailError) {
      bodyEl.appendChild(
        renderError({
          message: _reviewState.detailError,
          actionLabel: "Retry",
          onAction: function () { selectEntry(_reviewState.selectedEntry); },
        })
      );
      return;
    }

    var d = _reviewState.detail;
    if (!d) return;

    bodyEl.appendChild(
      el("div", { class: "inspector-section" }, [
        el("h3", { text: "Page" }),
        el("p", { class: "review-page-meta", text: "Page " + d.page_number }),
        el("p", {
          class: "review-page-meta",
          text: "Engine / model: " + (d.engine_name || "—") + " / " + (d.model_version || "—"),
        }),
        el("p", { class: "review-page-meta", text: "Text source: " + (d.text_source || "none") }),
      ])
    );

    var decisionSection = el("div", { class: "inspector-section" }, [el("h3", { text: "Decision" })]);
    decisionSection.appendChild(el("p", {}, [_decisionPill(d.decision)]));
    decisionSection.appendChild(
      el("p", {
        text: _reasonsSummary(d.reasons),
        style: "color:var(--color-text-muted);font-size:12px;margin-top:4px",
      })
    );
    bodyEl.appendChild(decisionSection);

    var warnings = [];
    if (d.page_ocr_error) warnings.push("OCR error: " + d.page_ocr_error);
    if (d.text_truncated) warnings.push("Text truncated for display — open the file to see all of it.");
    if (warnings.length > 0) {
      var warnSection = el("div", { class: "inspector-section" }, [el("h3", { text: "Warnings" })]);
      warnings.forEach(function (w) {
        warnSection.appendChild(
          el("p", { text: w, style: "color:var(--color-warning);font-size:12px" })
        );
      });
      bodyEl.appendChild(warnSection);
    }

    var textSection = el("div", { class: "inspector-section" }, [el("h3", { text: "Extracted text" })]);
    if (d.resolved_text && d.resolved_text.length > 0) {
      var pre = el("div", {
        class: "review-inspector-text",
        tabindex: "0",
        role: "region",
        "aria-label": "Extracted page text",
      });
      pre.textContent = d.resolved_text;
      textSection.appendChild(pre);
    } else {
      textSection.appendChild(
        el("p", {
          text: "No extracted text was resolved for this page.",
          style: "color:var(--color-text-muted);font-size:12px",
        })
      );
    }
    bodyEl.appendChild(textSection);
  }

  function renderList() {
    body.innerHTML = "";
    if (_reviewState.entries.length === 0) {
      body.appendChild(
        renderEmpty({
          title: "Nothing flagged for review.",
          detail: "Every indexed page either auto-accepted or hasn't been assessed yet.",
        })
      );
      return;
    }
    var groups = {};
    _reviewState.entries.forEach(function (entry) {
      var key = String(entry.file_id);
      if (!groups[key]) groups[key] = [];
      groups[key].push(entry);
    });
    Object.keys(groups).forEach(function (key) {
      var entries = groups[key];
      var fileCard = el("div", { class: "card review-file-card" });
      fileCard.appendChild(el("div", { style: "font-weight:600;margin-bottom:8px" }, [
        el("span", { text: truncateMiddle(entries[0].file_path, 90), title: entries[0].file_path }),
        el("span", { class: "review-page-meta", text: " · " + entries.length + " flagged page" + (entries.length === 1 ? "" : "s") }),
      ]));
      entries.forEach(function (entry) {
        fileCard.appendChild(_reviewRow(entry, {
          selected: _reviewState.selected === entry.page_review_id,
          onSelect: selectEntry,
          reload: reload,
          compact: true,
        }));
      });
      body.appendChild(fileCard);
    });
  }

  async function reload() {
    var res = await api("list_review_queue");
    if (!res.ok) {
      body.innerHTML = "";
      body.appendChild(renderError({ message: "Could not load the review queue." }));
      return;
    }
    _reviewState.entries = res.data;
    if (
      _reviewState.selected !== null &&
      !res.data.some(function (e) { return e.page_review_id === _reviewState.selected; })
    ) {
      _reviewState.selected = null;
      _reviewState.selectedEntry = null;
      _reviewState.detail = null;
    }
    renderList();
    renderInspector();
  }

  await reload();
}

function _reviewRow(entry, ctx) {
  var card = el("div", {
    class: "card review-card" + (ctx.selected ? " selected" : ""),
    tabindex: "0",
    role: "group",
    "aria-label":
      "Review " + (entry.file_path || "").split("/").pop() + " page " + entry.page_number,
  });

  function activate() { ctx.onSelect(entry); }
  card.addEventListener("click", function (e) {
    if (e.target.closest("button")) return; // action buttons handle themselves
    activate();
  });
  card.addEventListener("keydown", function (e) {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      activate();
    }
  });

  card.appendChild(
    el("div", { style: "display:flex;justify-content:space-between;align-items:center;gap:12px" }, [
      el("div", {}, [
        el("div", { text: truncateMiddle(entry.file_path, 70), style: "font-weight:600" }),
        el("div", {
          text: "Page " + entry.page_number + (entry.engine_name ? " · " + entry.engine_name : ""),
          class: "review-page-meta",
        }),
      ]),
      _decisionPill(entry.decision),
    ])
  );

  card.appendChild(
    el("p", {
      text: _reasonsSummary(entry.reasons),
      style: "color:var(--color-text-muted);font-size:12px;margin:8px 0",
    })
  );

  // Requirement 1: the full source path is selectable text.
  if (!ctx.compact) card.appendChild(el("p", { class: "mono-path", text: entry.file_path }));

  var resultEl = el("span", {
    style: "font-size:12px;color:var(--color-text-muted)",
    role: "status",
  });

  var copyBtn = el("button", {
    class: "secondary sm",
    type: "button",
    text: "Copy path",
    onclick: async function (e) {
      e.stopPropagation();
      var ok = await _copyToClipboard(entry.file_path);
      if (ok) {
        resultEl.textContent = "Path copied.";
        toast("Full path copied to the clipboard.");
      } else {
        resultEl.textContent = "Could not copy — select the path and copy it manually.";
        toast("Could not copy the path automatically.");
      }
    },
  });

  var openBtn = el("button", {
    class: "secondary sm",
    type: "button",
    text: "Open in Folder",
    onclick: async function (e) {
      e.stopPropagation();
      var res = await api("reveal_review_file", entry.file_id);
      if (res.ok) {
        resultEl.textContent = "Revealed in Finder.";
      } else {
        resultEl.textContent = (res.error && res.error.message) || "Could not reveal the file.";
      }
    },
  });

  var chatgptBtn = el("button", { class: "secondary sm", type: "button", text: "Prepare for ChatGPT" });
  var claudeBtn = el("button", { class: "secondary sm", type: "button", text: "Prepare for Claude" });

  async function prepareFor(provider) {
    chatgptBtn.disabled = true;
    claudeBtn.disabled = true;
    resultEl.textContent = "Preparing local package…";
    var res = await api("prepare_review_handoff", entry.file_id, [entry.page_number], provider);
    chatgptBtn.disabled = false;
    claudeBtn.disabled = false;
    if (res.ok) {
      var imageCount = (res.data.page_artifact_paths || []).length;
      resultEl.textContent = "Prepared locally: " + res.data.manifest_path;
      toast(
        "Local package ready (" + imageCount + " page image + OCR text + prompt). " +
          "Nothing was uploaded or sent to " + provider + " -- open the files yourself."
      );
    } else {
      resultEl.textContent = "";
    }
  }
  chatgptBtn.addEventListener("click", function (e) { e.stopPropagation(); prepareFor("chatgpt"); });
  claudeBtn.addEventListener("click", function (e) { e.stopPropagation(); prepareFor("claude"); });

  var removeBtn = el("button", {
    class: "danger sm",
    type: "button",
    text: "Remove from VectorDrive",
    onclick: function (e) {
      e.stopPropagation();
      confirmDialog({
        title: "Remove from VectorDrive",
        body:
          "This excludes \"" + (entry.file_path || "").split("/").pop() + "\" from future " +
          "indexing and deletes VectorDrive's index, review and handoff data for it, plus the " +
          "generated *.vectordrive.md sidecar. Your original source file is never deleted or " +
          "modified.",
        confirmLabel: "Remove",
        danger: true,
        onConfirm: async function () {
          removeBtn.disabled = true;
          resultEl.textContent = "Removing from VectorDrive…";
          try {
            var res = await api("remove_file_from_vectordrive", entry.file_id);
            if (res.ok) {
              toast(
                "Removed from VectorDrive. The source file was not touched" +
                  (res.data.sidecar_removed ? "; the generated sidecar was deleted." : ".")
              );
              await ctx.reload();
            } else {
              resultEl.textContent =
                (res.error && res.error.message) || "Could not remove the file.";
            }
          } catch (err) {
            resultEl.textContent = "Could not remove the file.";
          } finally {
            removeBtn.disabled = false;
          }
        },
      });
    },
  });

  card.appendChild(
    el("div", { class: "review-actions" }, [copyBtn, openBtn, chatgptBtn, claudeBtn, removeBtn, resultEl])
  );

  return card;
}

VIEW_RENDERERS.review = renderReview;
