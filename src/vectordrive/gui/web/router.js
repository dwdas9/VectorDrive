// Minimal hash router. No framework — location.hash IS the route.
"use strict";

const ROUTES = ["folders", "search", "chat", "overview", "review", "logs", "settings", "help", "claude"];
const VIEW_RENDERERS = {}; // populated by views/*.js: VIEW_RENDERERS.overview = render

function currentRoute() {
  const hash = (location.hash || "#/folders").replace(/^#\//, "");
  return ROUTES.includes(hash) ? hash : "folders";
}

function navigate(route) {
  if (location.hash !== `#/${route}`) location.hash = `#/${route}`;
  else render();
}

function render() {
  const route = currentRoute();
  document.querySelectorAll(".nav-item[data-route]").forEach((a) => {
    const active = a.dataset.route === route;
    a.classList.toggle("active", active);
    if (active) a.setAttribute("aria-current", "page");
    else a.removeAttribute("aria-current");
  });

  const main = document.getElementById("main");
  main.innerHTML = "";
  main.focus({ preventScroll: true });

  const renderer = VIEW_RENDERERS[route];
  if (renderer) renderer(main);
  else main.appendChild(renderEmpty({ title: `Unknown route: ${route}` }));

  if (window.pywebview && window.pywebview.api) {
    window.pywebview.api.save_ui_state({ last_route: route }).catch(() => {});
  }
}

window.addEventListener("hashchange", render);
