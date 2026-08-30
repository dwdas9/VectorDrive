"""Creates and runs the pywebview window. This is the only module allowed
to import `webview` — and even here the import is deferred inside run()
so importing vectordrive.gui.app itself never requires the `gui` extra to
be installed (cli/main.py imports cli/gui_cmd.py, which imports this
module, unconditionally at startup — that chain must stay import-safe).
"""
from __future__ import annotations

import html as html_lib
import os
import sys
import threading
import time
import traceback
from pathlib import Path

from vectordrive.config import get_data_home
from vectordrive.gui.api import GuiApi
from vectordrive.gui.state import load_state, save_state
from vectordrive.services.settings_service import setup_file_logging

MIN_WIDTH = 960
MIN_HEIGHT = 640

# Keep this short because the closing callback runs synchronously on the
# macOS UI thread. Cooperative jobs normally unwind almost immediately;
# work inside a non-interruptible library call gets only this brief grace
# period before _on_closed's deterministic process exit takes over.
CLOSE_CANCEL_TIMEOUT_S = 1.0


def _web_dir() -> Path:
    """Directory containing index.html and the rest of the frontend.

    G12 finding: this module (`vectordrive/gui/app.py`) is PyInstaller's
    *entry-point script* (see scripts/build_app.py), and PyInstaller
    flattens the entry script to the top level of the frozen bundle
    (`sys._MEIPASS`) — disconnected from its original package path.
    `Path(__file__).parent` for the frozen entry script resolves to
    `sys._MEIPASS` itself, not `sys._MEIPASS/vectordrive/gui`, so the old
    `WEB_DIR = Path(__file__).parent / "web"` silently pointed at
    `sys._MEIPASS/web` — a directory that doesn't exist. The bundled web
    assets actually land at the package-relative path build_app.py's
    `--add-data` preserves: `sys._MEIPASS/vectordrive/gui/web`. Loading a
    `file://` URL to a missing index.html produces no Python exception at
    all (it's a WebKit-level load failure) — just a silent blank white
    window, invisible without devtools. In dev mode (not frozen),
    `__file__` behaves normally and this falls back to the old logic.
    """
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS) / "vectordrive" / "gui" / "web"  # type: ignore[attr-defined]
    return Path(__file__).parent / "web"


WEB_DIR = _web_dir()


def _index_html_url() -> str:
    """file:// URL to index.html.

    G3 finding (superseding one detail of docs/gui.md's G0 spike): loading
    via html=<string> does NOT resolve the page's own relative
    href="styles.css" / src="app.js" references — those assets simply
    never load, leaving an unstyled, non-interactive page. G0's spike
    never caught this because its single-file test page had no external
    assets to resolve. A file:// URL is the standard, well-supported
    pywebview pattern for a real multi-file frontend and resolves
    relative paths exactly like a browser would; it is still local file
    access, not an HTTP server, so G0's "no internally-reachable web
    server" finding still holds (reverified for this mode in G3).
    """
    return (WEB_DIR / "index.html").resolve().as_uri()


def _startup_error_html(title: str, detail: str) -> str:
    """Self-contained inline error page for GUI bootstrap failures.

    G12 finding: a packaged .app opened from Finder has no visible
    stdout/stderr, so a caught-and-printed exception is exactly as silent
    to the user as an unhandled one — both used to produce a blank or
    absent window with zero user-facing signal. This page has zero
    external dependencies (no <link>/<script> src) so it renders even
    when the bundle's own web assets are the thing that's broken.
    """
    safe_title = html_lib.escape(title)
    safe_detail = html_lib.escape(detail)
    return f"""<!doctype html>
<html><head><meta charset="utf-8">
<title>VectorDrive — Startup Error</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, sans-serif; background: #1e1e1e;
          color: #e6e6e6; margin: 0; padding: 32px; }}
  h1 {{ color: #ff6b6b; font-size: 18px; margin: 0 0 12px; }}
  p {{ font-size: 14px; line-height: 1.5; }}
  pre {{ white-space: pre-wrap; word-break: break-word; background: #2a2a2a; padding: 16px;
         border-radius: 6px; font-size: 12px; line-height: 1.5; color: #ccc; }}
</style></head>
<body>
<h1>VectorDrive failed to start</h1>
<p>{safe_title}</p>
<pre>{safe_detail}</pre>
</body></html>"""


_menu_delegate = None  # retained for app lifetime
_menu_delegate_class = None  # Objective-C classes may only be registered once


def _setup_macos_menus(window) -> None:
    """Install native macOS menu bar with functional delegate. No-op on non-macOS."""
    global _menu_delegate, _menu_delegate_class
    if sys.platform != "darwin":
        return
    try:
        import AppKit
        import objc
        from PyObjCTools import AppHelper
    except ImportError:
        return

    def _js(code: str) -> None:
        try:
            window.evaluate_js(code)
        except Exception:  # noqa: BLE001
            pass

    def menuAction_(self, sender) -> None:  # noqa: N802
        tag = sender.tag()
        actions = {
            # View navigation
            100: 'navigate("folders")',
            101: 'navigate("search")',
            102: 'navigate("overview")',
            103: 'navigate("logs")',
            104: 'navigate("settings")',
            105: 'navigate("help")',
            106: 'navigate("chat")',
            # File menu
            200: "navigate('folders'); setTimeout(function(){var b=document.querySelector('[data-action=add-folder]'); if(b) b.click();}, 100)",
            # Search menu
            400: "navigate('search'); setTimeout(function(){var i=document.querySelector('input[type=search]'); if(i) i.focus();}, 50)",
            401: 'navigate("search"); setTimeout(function(){var bs=document.querySelectorAll(".segmented button"); if(bs[0]) bs[0].click();}, 50)',
            402: 'navigate("search"); setTimeout(function(){var bs=document.querySelectorAll(".segmented button"); if(bs[1]) bs[1].click();}, 50)',
            403: 'navigate("search"); setTimeout(function(){var bs=document.querySelectorAll(".segmented button"); if(bs[2]) bs[2].click();}, 50)',
            # Help
            500: 'navigate("help")',
            501: (
                'navigate("help"); setTimeout(function(){'
                'var i=document.getElementById("help-filter-input"); '
                'if(i){i.value="troubleshoot"; i.dispatchEvent(new Event("input"));}'
                '}, 50)'
            ),
            502: 'navigate("logs")',
            # About
            600: "showAboutDialog()",
        }
        js = actions.get(tag)
        if js:
            # NSMenu invokes actions on AppKit's main thread while its menu
            # tracking loop is still active.  Calling pywebview.evaluate_js
            # synchronously from that callback deadlocks: WebKit needs the
            # same UI thread to complete the bridge request.  Return from the
            # menu action immediately and let pywebview marshal the request
            # from a worker thread instead.
            threading.Thread(
                target=_js,
                args=(js,),
                name="vectordrive-menu-js",
                daemon=True,
            ).start()

    if _menu_delegate_class is None:
        # PyObjC's supported Python subclass construction works in both the
        # development environment and the frozen bundle.  ``objc.createClass``
        # is not part of the installed PyObjC API and caused a non-fatal
        # traceback every time the packaged window was shown.
        menu_action = objc.selector(menuAction_, signature=b"v@:@")
        _menu_delegate_class = type(
            "VDMenuDelegate",
            (AppKit.NSObject,),
            {"menuAction_": menu_action},
        )
    MenuDelegate = _menu_delegate_class  # noqa: N806

    def _make_menu() -> None:
        global _menu_delegate
        delegate = MenuDelegate.alloc().init()
        _menu_delegate = delegate  # prevent GC

        sel = delegate.menuAction_

        def _item(title, tag, key=""):
            item = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, sel, key)
            item.setTarget_(delegate)
            item.setTag_(tag)
            return item

        main_menu = AppKit.NSMenu.alloc().init()

        # -- VectorDrive (app) menu --
        app_menu = AppKit.NSMenu.alloc().initWithTitle_("VectorDrive")
        app_menu.addItem_(_item("About VectorDrive", 600))
        app_menu.addItem_(AppKit.NSMenuItem.separatorItem())
        app_menu.addItem_(_item("Settings…", 104, ","))
        app_menu.addItem_(AppKit.NSMenuItem.separatorItem())
        app_menu.addItem_(AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Quit VectorDrive", "terminate:", "q"))
        app_item = AppKit.NSMenuItem.alloc().init()
        app_item.setSubmenu_(app_menu)
        main_menu.addItem_(app_item)

        # -- File --
        file_menu = AppKit.NSMenu.alloc().initWithTitle_("File")
        file_menu.addItem_(_item("Add Folder…", 200, "o"))
        file_menu.addItem_(AppKit.NSMenuItem.separatorItem())
        file_menu.addItem_(AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Close Window", "performClose:", "w"))
        file_item = AppKit.NSMenuItem.alloc().init()
        file_item.setSubmenu_(file_menu)
        main_menu.addItem_(file_item)

        # -- Edit --
        edit_menu = AppKit.NSMenu.alloc().initWithTitle_("Edit")
        edit_menu.addItem_(AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Undo", "undo:", "z"))
        edit_menu.addItem_(AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Redo", "redo:", "Z"))
        edit_menu.addItem_(AppKit.NSMenuItem.separatorItem())
        edit_menu.addItem_(AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Cut", "cut:", "x"))
        edit_menu.addItem_(AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Copy", "copy:", "c"))
        edit_menu.addItem_(AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Paste", "paste:", "v"))
        edit_menu.addItem_(AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Select All", "selectAll:", "a"))
        edit_item = AppKit.NSMenuItem.alloc().init()
        edit_item.setSubmenu_(edit_menu)
        main_menu.addItem_(edit_item)

        # -- View --
        view_menu = AppKit.NSMenu.alloc().initWithTitle_("View")
        view_menu.addItem_(_item("Folders", 100, "1"))
        view_menu.addItem_(_item("Search", 101, "2"))
        view_menu.addItem_(_item("Chat", 106, "3"))
        view_menu.addItem_(_item("Overview", 102, "4"))
        view_menu.addItem_(_item("Logs", 103, "5"))
        view_menu.addItem_(AppKit.NSMenuItem.separatorItem())
        view_menu.addItem_(_item("Settings", 104, ","))
        view_item = AppKit.NSMenuItem.alloc().init()
        view_item.setSubmenu_(view_menu)
        main_menu.addItem_(view_item)

        # -- Search --
        search_menu = AppKit.NSMenu.alloc().initWithTitle_("Search")
        search_menu.addItem_(_item("Focus Search", 400, "k"))
        search_menu.addItem_(AppKit.NSMenuItem.separatorItem())
        search_menu.addItem_(_item("Hybrid Mode", 401))
        search_menu.addItem_(_item("FTS Mode", 402))
        search_menu.addItem_(_item("Vector Mode", 403))
        search_item = AppKit.NSMenuItem.alloc().init()
        search_item.setSubmenu_(search_menu)
        main_menu.addItem_(search_item)

        # -- Window --
        window_menu = AppKit.NSMenu.alloc().initWithTitle_("Window")
        window_menu.addItem_(AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Minimize", "performMiniaturize:", "m"))
        window_menu.addItem_(AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Zoom", "performZoom:", ""))
        window_item = AppKit.NSMenuItem.alloc().init()
        window_item.setSubmenu_(window_menu)
        main_menu.addItem_(window_item)

        # -- Help --
        help_menu = AppKit.NSMenu.alloc().initWithTitle_("Help")
        help_menu.addItem_(_item("VectorDrive Help", 500, "?"))
        help_menu.addItem_(_item("Troubleshooting", 501))
        help_menu.addItem_(_item("View Logs", 502))
        help_item = AppKit.NSMenuItem.alloc().init()
        help_item.setSubmenu_(help_menu)
        main_menu.addItem_(help_item)

        AppKit.NSApp.setMainMenu_(main_menu)

    AppHelper.callAfter(_make_menu)


def _run_with_webview(webview) -> int:  # noqa: ANN001 - `webview` module, imported lazily by caller
    data_home = get_data_home()
    setup_file_logging(data_home)
    state = load_state(data_home)
    api = GuiApi(data_home=data_home)

    window_kwargs = dict(
        width=max(state.window_width, MIN_WIDTH),
        height=max(state.window_height, MIN_HEIGHT),
        min_size=(MIN_WIDTH, MIN_HEIGHT),
    )

    index_path = WEB_DIR / "index.html"
    if index_path.exists():
        window = webview.create_window(
            "VectorDrive", url=_index_html_url(), js_api=api, **window_kwargs
        )
        exit_code = 0
    else:
        # Root-cause bug closed off at the source (_web_dir() above); this
        # branch is the last-resort visible fallback in case packaging
        # ever regresses and drops the web assets again.
        print(f"error: GUI web assets not found at {index_path}", file=sys.stderr)
        window = webview.create_window(
            "VectorDrive — Startup Error",
            html=_startup_error_html(
                "The application's interface files could not be found.",
                f"Expected index.html at:\n{index_path}\n\n"
                "This is a packaging bug — the .app bundle is missing its "
                "web assets. Please report this path.",
            ),
            **window_kwargs,
        )
        exit_code = 1

    api.window = window

    def _on_closing() -> bool:
        # window.events.closing fires synchronously on the main thread,
        # *before* macOS actually closes the window (pywebview's Event
        # class runs "should_lock" listeners inline, not on a spawned
        # thread). Tell an active job to stop and give cooperative work a
        # short chance to close its DB connection. Do not call back into
        # JavaScript here: evaluate_js is synchronous too and a stalled
        # bridge would turn a best-effort message into another unbounded
        # shutdown wait. Never veto the close (always return True): the
        # process-level shutdown in _on_closed below guarantees the exit.
        active = api.jobs.active_job_id()
        active_chat = api.chat_jobs.active_chat_id()
        if active is not None:
            api.jobs.cancel(active)
        if active_chat is not None:
            api.chat_jobs.cancel(active_chat)
        deadline = time.monotonic() + CLOSE_CANCEL_TIMEOUT_S
        if active is not None:
            api.jobs.wait_until_idle(max(0.0, deadline - time.monotonic()))
        if active_chat is not None:
            api.chat_jobs.wait_until_idle(max(0.0, deadline - time.monotonic()))
        return True

    def _on_closed() -> None:
        # docs/gui.md's G0 finding: window.destroy() alone did not
        # reliably end webview.start()'s run loop on this backend — the
        # documented `closed` event is the proven-reliable shutdown
        # signal. Best-effort geometry persistence; never let a save
        # failure block shutdown.
        try:
            width = getattr(window, "width", None) or state.window_width
            height = getattr(window, "height", None) or state.window_height
            state.window_width = int(width)
            state.window_height = int(height)
            save_state(data_home, state)
        except Exception:  # noqa: BLE001
            pass
        api.chat_jobs.shutdown()
        api.jobs.shutdown()

        # Confirmed root cause (see docs/CURRENT_CHECKPOINT.md's shutdown-
        # hang section): pywebview's Cocoa backend stops the NSApplication
        # run loop via NSApp.stop_(), which only sets a flag checked on
        # the *next* run-loop pass — if nothing else wakes it, that pass
        # never comes and webview.start() (and this whole process, since
        # pywebview dispatches `closed` listeners on a non-daemon thread
        # it never joins) hangs indefinitely. This is the G0 spike's own
        # documented "hard requirement" ("exiting there" from the closed
        # handler) that the previous implementation never actually did.
        # A hard, deterministic exit instead of hoping the run loop
        # notices: safe here because every write in this app goes through
        # SQLite in WAL mode (services/locking.py relies on the same
        # crash-safety property for lock release on kill -9) — an
        # abruptly killed writer leaves the WAL in a recoverable state,
        # never a corrupt one.
        os._exit(exit_code)

    window.events.closing += _on_closing
    window.events.closed += _on_closed
    window.events.shown += lambda: _setup_macos_menus(window)

    # Verification-only hook (never set by a normal launch): drives a
    # real indexing job through the real window/JobRunner/Cocoa-closing
    # path, then closes the window itself — used to prove the shutdown
    # fix against the actual pywebview runtime without needing
    # Accessibility-permission-gated UI-click automation. See
    # docs/CURRENT_CHECKPOINT.md's shutdown-hang verification section.
    autotest_folder = os.environ.get("VECTORDRIVE_GUI_AUTOTEST_FOLDER")
    if autotest_folder:
        import threading

        def _autotest_drive() -> None:
            window.events.shown.wait(10)
            add_res = api.add_folder(autotest_folder)
            if not add_res["ok"]:
                print(f"AUTOTEST add_folder failed: {add_res}", file=sys.stderr)
                window.destroy()
                return
            index_res = api.start_index(autotest_folder, mode="incremental")
            job_id = index_res["data"]["job_id"]
            close_during_index_s = os.environ.get(
                "VECTORDRIVE_GUI_AUTOTEST_CLOSE_DURING_INDEX_S"
            )
            if close_during_index_s is not None:
                time.sleep(float(close_during_index_s))
                print(
                    f"AUTOTEST closing during active index job {job_id}",
                    file=sys.stderr,
                )
                window.destroy()
                return
            deadline = time.time() + 120
            job = None
            while time.time() < deadline:
                job = api.get_job(job_id)
                if job["data"] and job["data"]["status"] in ("done", "failed", "cancelled"):
                    break
                time.sleep(0.5)
            print(f"AUTOTEST index result: {job}", file=sys.stderr)
            time.sleep(float(os.environ.get("VECTORDRIVE_GUI_AUTOTEST_HOLD_S", "2")))
            window.destroy()

        threading.Thread(target=_autotest_drive, daemon=True).start()

    webview.start(debug=False)
    return exit_code


def run() -> int:
    try:
        import webview
    except ImportError as exc:
        print(
            f"The GUI requires the 'gui' extra. Install it with:\n"
            f'  pip install "vectordrive[gui]"\n'
            f"  (import error: {exc})",
            file=sys.stderr,
        )
        return 1

    try:
        return _run_with_webview(webview)
    except Exception:  # noqa: BLE001 - last-resort visible failure, see _startup_error_html
        detail = traceback.format_exc()
        print(detail, file=sys.stderr)
        try:
            webview.create_window(
                "VectorDrive — Startup Error",
                html=_startup_error_html(
                    "VectorDrive hit an unexpected error while starting.", detail
                ),
                width=MIN_WIDTH,
                height=MIN_HEIGHT,
                min_size=(MIN_WIDTH, MIN_HEIGHT),
            )
            webview.start(debug=False)
        except Exception:  # noqa: BLE001
            pass
        return 1


def _selftest(folder: str, tokens: list[str]) -> int:
    """Headless smoke test for the packaged bundle: adds `folder`, indexes
    it once, then runs fts/vector/hybrid search for each of `tokens`
    (one per fixture file expected in `folder`). No GUI window, no
    `import webview` — exercises exactly the frozen bundle's OCR/search
    stack (proving cv2/onnxruntime/rapidocr actually work once bundled;
    see scripts/build_app.py) without driving the UI interactively.
    Invoke as: VectorDrive --selftest <folder> <token1> [token2 ...]
    (VECTORDRIVE_HOME env var controls which data home is used).
    """
    import json
    import time

    data_home = get_data_home()
    api = GuiApi(data_home=data_home)
    report: dict = {"folder": folder, "tokens": tokens}

    report["add_folder"] = add_result = api.add_folder(folder)
    if not add_result["ok"]:
        print(json.dumps(report, indent=2, default=str))
        return 1

    report["start_index"] = index_result = api.start_index(folder, mode="incremental")
    if not index_result["ok"]:
        print(json.dumps(report, indent=2, default=str))
        return 1
    job_id = index_result["data"]["job_id"]

    deadline = time.time() + 180
    job = None
    while time.time() < deadline:
        job_result = api.get_job(job_id)
        job = job_result["data"] if job_result["ok"] else job_result
        if job and job.get("status") in ("done", "failed", "cancelled"):
            break
        time.sleep(1)
    report["index_job"] = job

    report["searches"] = {
        token: {mode: api.search(token, mode=mode, top_k=5) for mode in ("fts", "vector", "hybrid")}
        for token in tokens
    }

    print(json.dumps(report, indent=2, default=str))
    ok = bool(job and job.get("status") == "done")
    return 0 if ok else 1


def _db_counts(data_home) -> dict:  # noqa: ANN001 - Path, kept untyped to avoid importing pathlib just for the hint
    """Direct sqlite3 row counts for the tables a folder purge must clear.
    Queries sqlite-vec's shadow table (vec_chunks_chunks) by name rather
    than the vec_chunks virtual table itself, so this works without
    loading the sqlite-vec extension — same technique used to inspect the
    G13 bug's failed database by hand.
    """
    import sqlite3

    from vectordrive.config import get_db_path

    conn = sqlite3.connect(get_db_path(data_home))
    counts = {}
    for table in ("files", "pages", "extracted_docs", "chunks", "fts_chunks", "vec_chunks_chunks"):
        try:
            counts[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        except sqlite3.OperationalError as exc:
            counts[table] = f"error: {exc}"
    conn.close()
    return counts


def _hash_files_under(folder) -> dict:  # noqa: ANN001
    import hashlib
    from pathlib import Path

    hashes = {}
    for path in Path(folder).rglob("*"):
        if path.is_file():
            hashes[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
    return hashes


def _selftest_removal(folder: str, tokens: list[str]) -> int:
    """G13 regression smoke test for the packaged bundle: index `folder`,
    confirm each of `tokens` is searchable (fts/vector/hybrid), remove the
    folder with purge, then confirm: the tokens are no longer searchable
    in any mode, the files/chunks/fts_chunks/vec_chunks_chunks row counts
    dropped to what they were before indexing, Overview reports zero
    indexed files, and every source file under `folder` still exists on
    disk with an unchanged content hash. No GUI window, no `import
    webview`. Invoke as:
    VectorDrive --selftest-removal <folder> <token1> [token2 ...]
    (VECTORDRIVE_HOME env var controls which data home is used; run
    --selftest-search-only against the same VECTORDRIVE_HOME afterward,
    as a fresh process, to additionally confirm removal survives a
    close-and-reopen of the app.)
    """
    import json
    import time

    data_home = get_data_home()
    api = GuiApi(data_home=data_home)
    report: dict = {"folder": folder, "tokens": tokens, "checks": {}}

    source_hashes_before = _hash_files_under(folder)
    report["db_counts_before_index"] = _db_counts(data_home)

    report["add_folder"] = add_result = api.add_folder(folder)
    if not add_result["ok"]:
        print(json.dumps(report, indent=2, default=str))
        return 1

    report["start_index"] = index_result = api.start_index(folder, mode="incremental")
    if not index_result["ok"]:
        print(json.dumps(report, indent=2, default=str))
        return 1
    job_id = index_result["data"]["job_id"]

    def _await_job(job_id):
        deadline = time.time() + 180
        job = None
        while time.time() < deadline:
            job_result = api.get_job(job_id)
            job = job_result["data"] if job_result["ok"] else job_result
            if job and job.get("status") in ("done", "failed", "cancelled"):
                break
            time.sleep(1)
        return job

    index_job = _await_job(job_id)
    report["index_job"] = index_job
    report["checks"]["indexing_succeeded"] = bool(index_job and index_job.get("status") == "done")

    report["searches_before_removal"] = {
        token: {mode: api.search(token, mode=mode, top_k=5) for mode in ("fts", "vector", "hybrid")}
        for token in tokens
    }
    report["checks"]["all_tokens_found_before_removal"] = all(
        len(report["searches_before_removal"][token][mode]["data"]["results"]) > 0
        for token in tokens
        for mode in ("fts", "vector", "hybrid")
    )
    report["db_counts_after_index"] = _db_counts(data_home)

    report["remove_folder"] = remove_result = api.remove_folder(folder, purge_index=True)
    report["checks"]["removal_succeeded"] = bool(remove_result["ok"])

    report["db_counts_after_removal"] = counts_after = _db_counts(data_home)
    before_index_counts = report["db_counts_before_index"]

    def _as_int(value):
        # Before the first index run, the database file (and therefore
        # these tables) may not exist yet at all — _db_counts() reports
        # that as an "error: no such table" string, which is the same
        # "nothing here" state as a genuine 0 once the schema exists.
        return value if isinstance(value, int) else 0

    report["checks"]["db_counts_returned_to_pre_index_levels"] = all(
        _as_int(counts_after.get(table)) == _as_int(before_index_counts.get(table))
        for table in ("files", "pages", "extracted_docs", "chunks", "fts_chunks", "vec_chunks_chunks")
    )

    report["searches_after_removal"] = {
        token: {mode: api.search(token, mode=mode, top_k=5) for mode in ("fts", "vector", "hybrid")}
        for token in tokens
    }
    report["checks"]["no_tokens_found_after_removal"] = all(
        len(report["searches_after_removal"][token][mode]["data"]["results"]) == 0
        for token in tokens
        for mode in ("fts", "vector", "hybrid")
    )

    report["overview_after_removal"] = overview = api.get_overview()
    status = (overview.get("data") or {}).get("status") or {}
    indexed_count = (status.get("file_status_counts") or {}).get("indexed", 0)
    report["checks"]["overview_reports_zero_indexed"] = indexed_count == 0

    source_hashes_after = _hash_files_under(folder)
    report["checks"]["source_files_untouched"] = source_hashes_after == source_hashes_before

    print(json.dumps(report, indent=2, default=str))
    return 0 if all(report["checks"].values()) else 1


def _selftest_search_only(tokens: list[str]) -> int:
    """Companion to --selftest-removal: run fresh searches against
    whatever is already in VECTORDRIVE_HOME's database, with no add/index
    step of its own. Run as a *separate process launch* of the same
    binary after --selftest-removal to prove a removal survives a real
    close-and-reopen of the app (VECTORDRIVE_HOME env var must be the
    same isolated home used for --selftest-removal).
    Invoke as: VectorDrive --selftest-search-only <token1> [token2 ...]
    """
    import json

    data_home = get_data_home()
    api = GuiApi(data_home=data_home)
    report: dict = {
        "tokens": tokens,
        "db_counts": _db_counts(data_home),
        "searches": {
            token: {mode: api.search(token, mode=mode, top_k=5) for mode in ("fts", "vector", "hybrid")}
            for token in tokens
        },
    }
    all_empty = all(
        len(report["searches"][token][mode]["data"]["results"]) == 0
        for token in tokens
        for mode in ("fts", "vector", "hybrid")
    )
    report["checks"] = {"still_absent_after_restart": all_empty}
    print(json.dumps(report, indent=2, default=str))
    return 0 if all_empty else 1


def _run_mcp_server() -> int:
    """Entry point for Claude Desktop (or any MCP client) to launch
    VectorDrive's read-only MCP server from the packaged app.

    G14 finding: a packaged .app has no separate `vectordrive` CLI
    script to point Claude Desktop at (services/claude_desktop.py's
    resolve_executable() previously always failed here — see its
    docstring) — this makes the frozen binary itself capable of running
    the exact same stdio MCP server `vectordrive mcp` runs in a normal
    install. No GUI, no webview.
    """
    from vectordrive.mcp.server import run_mcp_stdio_server

    run_mcp_stdio_server()
    return 0


def _selftest_mcp_connect() -> int:
    """Headless verification that the Claude Desktop screen's "Connect"
    flow — claude_detect() -> claude_preview_connect() -> claude_apply(),
    the exact GuiApi methods its buttons call — works when driven from
    inside the frozen bundle, where sys.frozen is set and sys.executable
    is this binary itself (the scenario resolve_executable()'s G14 fix
    targets). By default it uses the real Claude Desktop config path,
    matching the real Connect action. Release validation sets
    VECTORDRIVE_CLAUDE_CONFIG_PATH to an isolated temporary fixture so
    the same frozen-bundle path is proven without changing the user's
    live Claude configuration. No GUI, no webview. Invoke as:
    VectorDrive --selftest-mcp-connect
    (VECTORDRIVE_HOME env var controls which data home gets registered.)
    """
    import json

    data_home = get_data_home()
    api = GuiApi(data_home=data_home)
    config_override = os.environ.get("VECTORDRIVE_CLAUDE_CONFIG_PATH")
    if config_override:
        api._claude_config_path_override = Path(config_override)
    report: dict = {}

    report["detect_before"] = detect_before = api.claude_detect()
    report["preview"] = preview = api.claude_preview_connect()
    already_current = preview.get("ok") and (preview.get("data") or {}).get("is_noop")
    report["apply"] = None if already_current else (api.claude_apply(preview["data"]) if preview.get("ok") else None)
    report["detect_after"] = api.claude_detect()
    report["config_path"] = str(api._claude_config_path()) if api._claude_config_path() else None

    print(json.dumps(report, indent=2, default=str))
    ok = bool(detect_before.get("ok")) and bool(preview.get("ok"))
    return 0 if ok else 1


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "--version":
        import vectordrive

        print(f"VectorDrive {vectordrive.__version__}")
        sys.exit(0)
    if len(sys.argv) >= 2 and sys.argv[1] == "--selftest":
        if len(sys.argv) < 4:
            print("usage: VectorDrive --selftest <folder> <token1> [token2 ...]", file=sys.stderr)
            sys.exit(2)
        sys.exit(_selftest(sys.argv[2], sys.argv[3:]))
    if len(sys.argv) >= 2 and sys.argv[1] == "--selftest-removal":
        if len(sys.argv) < 4:
            print("usage: VectorDrive --selftest-removal <folder> <token1> [token2 ...]", file=sys.stderr)
            sys.exit(2)
        sys.exit(_selftest_removal(sys.argv[2], sys.argv[3:]))
    if len(sys.argv) >= 2 and sys.argv[1] == "--selftest-search-only":
        if len(sys.argv) < 3:
            print("usage: VectorDrive --selftest-search-only <token1> [token2 ...]", file=sys.stderr)
            sys.exit(2)
        sys.exit(_selftest_search_only(sys.argv[2:]))
    if len(sys.argv) >= 2 and sys.argv[1] == "--selftest-mcp-connect":
        sys.exit(_selftest_mcp_connect())
    if len(sys.argv) >= 2 and sys.argv[1] == "--mcp":
        sys.exit(_run_mcp_server())
    sys.exit(run())
