// Bootstrap: wait for the pywebview bridge, do the first render, start
// the event-poll loop. window.pywebview.api only exists after
// 'pywebviewready' fires.
"use strict";

let _eventCursor = 0;

async function api(method, ...args) {
  const result = await window.pywebview.api[method](...args);
  if (!result.ok) {
    const msg = result.error ? result.error.message : "Something went wrong.";
    toast(msg);
  }
  return result;
}

async function pollEventsLoop() {
  try {
    const res = await api("poll_events", _eventCursor);
    if (res.ok) {
      if (res.data.gap) {
        window.dispatchEvent(new CustomEvent("vd:event-gap"));
      }
      _eventCursor = res.data.cursor;
      for (const evt of res.data.events) {
        window.dispatchEvent(new CustomEvent("vd:event", { detail: evt }));
      }
    }
  } catch (e) {
    // A single failed poll is not fatal — the next tick tries again.
  }
  setTimeout(pollEventsLoop, 250);
}

function _setupKeyboardShortcuts() {
  document.addEventListener("keydown", (e) => {
    const meta = e.metaKey || e.ctrlKey;
    if (!meta) return;

    if (e.key === "k" || e.key === "K") {
      e.preventDefault();
      navigate("search");
      setTimeout(() => {
        const input = document.querySelector('input[type="search"]');
        if (input) input.focus();
      }, 50);
      return;
    }

    if (e.key === "1") { e.preventDefault(); navigate("folders"); return; }
    if (e.key === "2") { e.preventDefault(); navigate("search"); return; }
    if (e.key === "3") { e.preventDefault(); navigate("chat"); return; }
    if (e.key === "4") { e.preventDefault(); navigate("overview"); return; }
    if (e.key === "5") { e.preventDefault(); navigate("logs"); return; }
    if (e.key === ",") { e.preventDefault(); navigate("settings"); return; }
    if (e.key === "o" || e.key === "O") {
      e.preventDefault();
      navigate("folders");
      setTimeout(() => {
        const addBtn = document.querySelector("[data-action='add-folder']");
        if (addBtn) addBtn.click();
      }, 100);
      return;
    }
  });
}

function showAboutDialog() {
  const root = document.getElementById("dialog-root");
  if (root.children.length > 0) return;

  // Same brand SVG markup as index.html's .nav-brand-icon (just 64x64
  // instead of 20x20) — About shows the same product mark as the nav,
  // not a third hand-rolled icon.
  const iconWrap = el("div", { class: "about-icon" });
  iconWrap.innerHTML = '<svg width="64" height="64" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 4h3v3H4zM4 10h3v3H4zM4 16h3v3H4z" fill="currentColor" stroke="none" opacity="0.6"/><path d="M5.5 5.5L12 12M5.5 11.5L12 12M5.5 17.5L12 12"/><circle cx="12" cy="12" r="2" fill="#4ade80" stroke="none"/><path d="M12 12L19 5" stroke-width="2.5"/><path d="M17 5h2v2" stroke-width="2"/></svg>';

  const body = el("div", { class: "about-dialog" }, [
    iconWrap,
    el("p", { class: "about-name", text: "VectorDrive" }),
    el("p", { class: "about-version", id: "about-version", text: "Loading..." }),
    el("p", { class: "about-credit", text: "Created by DDas" }),
    el("p", { class: "about-desc", text: "Private, local document search and indexing." }),
    el("div", { style: "display:flex;gap:8px;justify-content:center" }, [
      el("button", { class: "secondary", text: "Close", onclick: () => overlay.remove() }),
      el("button", { class: "secondary", text: "Copy System Info", onclick: async () => {
        const info = await api("get_app_info");
        if (info.ok) {
          const text = `VectorDrive ${info.data.version}\nData: ${info.data.data_home}\nUptime: ${info.data.uptime_s}s`;
          try { navigator.clipboard.writeText(text); toast("Copied to clipboard."); }
          catch (err) { toast("Could not copy."); }
        }
      }}),
    ]),
  ]);

  const overlay = el("div", {
    role: "dialog", "aria-modal": "true",
    style: "position:absolute;top:0;right:0;bottom:0;left:0;pointer-events:auto;background:rgba(0,0,0,0.4);display:flex;align-items:center;justify-content:center",
  }, [body]);

  root.appendChild(overlay);

  api("get_app_info").then((res) => {
    if (res.ok) {
      const versionEl = document.getElementById("about-version");
      if (versionEl) versionEl.textContent = `Version ${res.data.version}`;
    }
  });
}

function boot() {
  document.querySelectorAll(".nav-item[data-route]").forEach((a) => {
    if (a.dataset.icon) a.prepend(icon(a.dataset.icon));
    a.addEventListener("click", (e) => {
      e.preventDefault();
      navigate(a.dataset.route);
    });
  });
  _setupKeyboardShortcuts();
  render();
  pollEventsLoop();
}

window.addEventListener("pywebviewready", boot);
