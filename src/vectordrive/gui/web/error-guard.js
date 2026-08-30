// G12 finding: a blank white window was previously indistinguishable
// from "still loading" — there was no visible signal for an HTML/JS
// asset failing to load, a script throwing during bootstrap, a rejected
// promise, or the pywebview bridge never becoming ready. This file is
// loaded FIRST (before components.js/router.js/views/*.js/app.js) so it
// can catch failures in all of them, including parse-time syntax errors
// in a later <script> tag. It has no dependency on any other file in
// this directory (no api() calls, no styles.css) so it renders even
// when everything else in the bundle is broken.
"use strict";

(function () {
  var shown = false;

  function showBanner(message) {
    if (shown) return;
    shown = true;
    var banner = document.createElement("div");
    banner.setAttribute("role", "alert");
    banner.style.cssText =
      "position:fixed;top:0;left:0;right:0;z-index:2147483647;" +
      "background:#b3261e;color:#fff;padding:16px 20px;" +
      "font:13px -apple-system,BlinkMacSystemFont,sans-serif;" +
      "white-space:pre-wrap;word-break:break-word;box-shadow:0 2px 8px rgba(0,0,0,.3);";
    banner.textContent = "VectorDrive ran into a problem: " + message;
    (document.body || document.documentElement).appendChild(banner);
  }

  window.addEventListener("error", function (e) {
    var detail = e && e.message ? e.message : "an unknown script error";
    if (e && e.filename) detail += " (" + e.filename + ":" + e.lineno + ")";
    showBanner(detail);
  });

  window.addEventListener("unhandledrejection", function (e) {
    var reason = e && e.reason ? (e.reason.message || String(e.reason)) : "a rejected promise";
    showBanner(reason);
  });

  // The rest of the app (app.js's boot()) only runs once
  // 'pywebviewready' fires. If the native bridge never signals ready —
  // e.g. js_api construction failed, or the bridge injection itself was
  // blocked — the page would otherwise sit blank forever with no signal
  // at all.
  var bridgeReady = false;
  window.addEventListener("pywebviewready", function () {
    bridgeReady = true;
  });
  window.setTimeout(function () {
    if (!bridgeReady) {
      showBanner(
        "the application backend did not respond. Try quitting and reopening VectorDrive."
      );
    }
  }, 8000);
})();
