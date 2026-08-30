// Shared, dependency-free DOM helpers. Every view (views/*.js) renders
// through these so the four states (empty/loading/error/success) look
// and behave consistently across screens — per the approved plan's
// accessibility + "no decorative clutter" requirements.
"use strict";

// Attributes that must be assigned as live DOM properties, never as string
// attributes: setAttribute("disabled", false) still disables the element
// (the attribute counts as present regardless of value, even "false"). Callers
// pass these as real booleans, e.g. el("button", { disabled: isBusy }).
const booleanAttrs = new Set(["disabled", "checked", "selected", "multiple", "required", "autofocus", "hidden"]);

function el(tag, attrs, children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (k === "text") node.textContent = v;
    else if (k.startsWith("on") && typeof v === "function") node.addEventListener(k.slice(2), v);
    else if (booleanAttrs.has(k)) node[k] = Boolean(v);
    else if (v !== null && v !== undefined) node.setAttribute(k, v);
  }
  for (const child of children || []) {
    if (child) node.appendChild(typeof child === "string" ? document.createTextNode(child) : child);
  }
  return node;
}

function renderEmpty({ title, detail, actionLabel, onAction }) {
  const children = [
    el("p", { text: title, style: "font-weight:600;margin:0 0 4px" }),
  ];
  if (detail) children.push(el("p", { text: detail, style: "margin:0 0 12px" }));
  if (actionLabel) children.push(el("button", { onclick: onAction, text: actionLabel }));
  return el("div", { class: "empty-state", role: "status" }, children);
}

function renderLoading(opts) {
  const label = (opts && opts.label) || "Loading…";
  return el("div", { "aria-busy": "true", "aria-live": "polite" }, [
    el("span", { class: "visually-hidden", text: label }),
    el("div", { class: "skeleton", style: "width:60%;margin-bottom:8px" }),
    el("div", { class: "skeleton", style: "width:90%;margin-bottom:8px" }),
    el("div", { class: "skeleton", style: "width:40%" }),
  ]);
}

function renderError({ message, hint, actionLabel, onAction }) {
  const children = [el("p", { text: message, style: "margin:0 0 4px;font-weight:600" })];
  if (hint) children.push(el("p", { text: hint, style: "margin:0 0 8px" }));
  if (actionLabel) children.push(el("button", { class: "secondary", onclick: onAction, text: actionLabel }));
  return el("div", { class: "error-banner", role: "alert" }, children);
}

function statusPill(status, text) {
  // status: "pass" | "warn" | "fail" — glyph + text, never color alone.
  return el("span", { class: `status-pill ${status}`, text: " " + text });
}

function progressBar(fraction) {
  const pct = Math.max(0, Math.min(1, fraction)) * 100;
  return el(
    "div",
    { class: "progress-bar", role: "progressbar", "aria-valuenow": String(Math.round(pct)), "aria-valuemin": "0", "aria-valuemax": "100" },
    [el("div", { class: "progress-bar-fill", style: `width:${pct}%` })]
  );
}

let _toastRegion = null;
function toast(message) {
  _toastRegion = _toastRegion || document.getElementById("toast-region");
  if (!_toastRegion) return;
  const node = el("div", { class: "toast", text: message });
  _toastRegion.appendChild(node);
  setTimeout(() => node.remove(), 5000);
}

function confirmDialog({ title, body, confirmLabel, onConfirm, danger }) {
  const root = document.getElementById("dialog-root");
  // G12 finding: a second click while a dialog is already open used to
  // stack a duplicate overlay on top of the first — both fully
  // interactive, neither visibly distinguishable from the other. Only
  // one dialog may be open at a time.
  if (root.children.length > 0) return;
  const overlay = el(
    "div",
    {
      role: "alertdialog",
      "aria-modal": "true",
      "aria-labelledby": "dlg-title",
      // position:absolute, not fixed — #dialog-root (styles.css) is
      // already the fixed, viewport-anchored container; see its G12
      // comment for why the fixed anchor must live there, not here,
      // and why this uses explicit top/right/bottom/left, not the
      // unsupported `inset` shorthand.
      style: "position:absolute;top:0;right:0;bottom:0;left:0;pointer-events:auto;background:rgba(0,0,0,0.4);display:flex;align-items:center;justify-content:center",
    },
    [
      el("div", { class: "card", style: "max-width:420px" }, [
        el("h2", { id: "dlg-title", text: title }),
        el("p", { text: body }),
        el("div", { style: "display:flex;gap:8px;justify-content:flex-end" }, [
          el("button", { class: "secondary", text: "Cancel", onclick: () => overlay.remove() }),
          el("button", {
            class: danger ? "danger" : "",
            text: confirmLabel,
            onclick: () => { overlay.remove(); onConfirm(); },
          }),
        ]),
      ]),
    ]
  );
  root.appendChild(overlay);
  overlay.querySelector("button").focus();
}

function resultList(items, renderItem) {
  if (!items.length) return el("p", { class: "empty-state", text: "No results." });
  return el("div", {}, items.map(renderItem));
}

function truncateMiddle(str, maxChars) {
  // Deterministic, unit-testable middle truncation for long folder/file
  // paths — not a CSS trick (styles.css's G4/G12 history shows dynamic
  // layout tricks are risky on this WKWebView build). The full path is
  // always still available via the native title="" tooltip and the
  // "Reveal" button.
  const limit = maxChars || 60;
  if (str.length <= limit) return str;
  const keep = limit - 1; // reserve one char for the ellipsis
  const head = Math.ceil(keep * 0.4);
  const tail = keep - head;
  return str.slice(0, head) + "…" + str.slice(str.length - tail);
}

function statusChip(status, name, message) {
  // status: "pass" | "warn" | "fail". The dot is additive decoration —
  // the glyph+text status-pill it sits beside is what actually carries
  // the color-blind-safe signal (see statusPill's "never color-only"
  // comment above).
  return el("div", { class: "chip" }, [
    el("div", { style: "display:flex;align-items:center" }, [
      el("span", { class: `chip-dot ${status}` }),
      statusPill(status, name.replace(/_/g, " ")),
    ]),
    message ? el("p", { text: message, style: "color:var(--color-text-muted);font-size:12px;margin:4px 0 0" }) : null,
  ]);
}

function statCallout(value, label) {
  return el("p", { class: "stat-callout" }, [
    el("span", { class: "stat-value", text: String(value) }),
    el("span", { class: "stat-label", text: label }),
  ]);
}

function segmentedControl(options, activeValue, onChange) {
  // options: [{value, label, title?}]. A single rounded track with a
  // sliding accent "thumb" behind the active label, replacing a
  // radio-button list. G4/Stage-8 finding: percent-based math
  // (width: 100/N%, translateX(i * 100%)) assumes every segment is the
  // same width, which stopped being true once labels of different
  // lengths (Hybrid/Keyword/Semantic) shared one control — the thumb
  // drifted out from under short/long labels. Measured positioning
  // (offsetWidth/offsetLeft, read from the actual rendered button) is
  // correct regardless of per-label width.
  const track = el("div", { class: "segmented", role: "radiogroup" });
  const thumb = el("div", { class: "segmented-thumb" });
  track.appendChild(thumb);

  function moveThumb(btn) {
    thumb.style.width = btn.offsetWidth + "px";
    thumb.style.transform = `translateX(${btn.offsetLeft - 2}px)`;
    // 2 = .segmented's own padding; offsetLeft is relative to the
    // position:relative track, so this lands the thumb exactly under
    // the button regardless of per-label width.
  }

  const buttons = options.map((opt) => {
    const attrs = {
      type: "button",
      role: "radio",
      "aria-checked": opt.value === activeValue ? "true" : "false",
      class: opt.value === activeValue ? "active" : "",
      text: opt.label,
    };
    if (opt.title) attrs.title = opt.title;
    const btn = el("button", attrs);
    btn.addEventListener("click", () => {
      track.querySelectorAll("button").forEach((b) => {
        b.classList.remove("active");
        b.setAttribute("aria-checked", "false");
      });
      btn.classList.add("active");
      btn.setAttribute("aria-checked", "true");
      moveThumb(btn);
      onChange(opt.value);
    });
    return btn;
  });
  buttons.forEach((b) => track.appendChild(b));

  requestAnimationFrame(() => {
    // After insertion into the DOM, so offsetWidth/offsetLeft are real.
    const active = track.querySelector("button.active") || buttons[0];
    if (active) moveThumb(active);
  });

  return track;
}

// Stage 5 (VECTORDRIVE_IMPLEMENTATION_PLAN.md): a "…" secondary button
// that toggles a small popover menu below it — Folders' per-row action
// hierarchy (Re-index/Relink primary + everything else tucked behind
// this) instead of a growing row of equal-weight buttons. Only one
// popover open at a time, tracked here module-level.
let _openMenuPopover = null;

function _closeMenuPopover() {
  if (!_openMenuPopover) return;
  var p = _openMenuPopover;
  _openMenuPopover = null;
  p.popover.remove();
  document.removeEventListener("click", p.onOutsideClick, true);
  document.removeEventListener("keydown", p.onKeydown, true);
  p.button.setAttribute("aria-expanded", "false");
}

function menuButton(items) {
  // items: [{label, onClick, danger?}] or the string "-" for a separator.
  var button = el("button", {
    class: "sm secondary", type: "button", text: "…",
    "aria-haspopup": "true", "aria-expanded": "false",
  });
  button.addEventListener("click", function (e) {
    e.stopPropagation();
    if (_openMenuPopover && _openMenuPopover.button === button) { _closeMenuPopover(); return; }
    _closeMenuPopover();

    var rect = button.getBoundingClientRect();
    var popover = el("div", { class: "menu-popover", role: "menu", style: `top:${rect.bottom + 4}px;left:${rect.right}px` });
    items.forEach(function (item) {
      if (item === "-") { popover.appendChild(el("hr")); return; }
      popover.appendChild(el("button", {
        type: "button", role: "menuitem",
        class: item.danger ? "danger" : "",
        text: item.label,
        onclick: function (ev) { ev.stopPropagation(); _closeMenuPopover(); item.onClick(); },
      }));
    });
    document.body.appendChild(popover);
    button.setAttribute("aria-expanded", "true");

    var onOutsideClick = function (ev) {
      if (!popover.contains(ev.target) && ev.target !== button) _closeMenuPopover();
    };
    var onKeydown = function (ev) {
      if (ev.key === "Escape") _closeMenuPopover();
    };
    // Deferred: this same click event is still bubbling (button's own
    // listener called stopPropagation() on it, but the capture-phase
    // listener below is added fresh) — without the deferral, the
    // document-level capture listener can see this very click and close
    // the popover it just opened.
    setTimeout(function () {
      document.addEventListener("click", onOutsideClick, true);
      document.addEventListener("keydown", onKeydown, true);
    }, 0);

    _openMenuPopover = { popover: popover, button: button, onOutsideClick: onOutsideClick, onKeydown: onKeydown };
  });
  return button;
}

function diffView(unifiedDiff) {
  const pre = el("pre", { style: "white-space:pre-wrap;font-size:12px;background:var(--color-surface);padding:12px;border-radius:8px;border:1px solid var(--color-border)" });
  pre.textContent = unifiedDiff;
  return pre;
}
