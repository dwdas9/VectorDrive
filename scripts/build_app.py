"""Build VectorDrive.app using PyInstaller.

Run from the project root with the venv active:
    .venv/bin/python scripts/build_app.py

Produces: dist/VectorDrive.app
"""
from __future__ import annotations

import ast
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
VENV_SITE = Path(sys.executable).parent.parent / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}" / "site-packages"


def _read_project_version(project_root: Path | None = None) -> str:
    """Read the version from the source tree being packaged.

    Importing ``vectordrive`` here can resolve an older editable installation
    from the build environment instead of this worktree.
    """
    root = project_root or PROJECT_ROOT
    version_file = root / "src" / "vectordrive" / "__init__.py"
    module = ast.parse(version_file.read_text(encoding="utf-8"), filename=str(version_file))
    for statement in module.body:
        if not isinstance(statement, ast.Assign):
            continue
        if not any(isinstance(target, ast.Name) and target.id == "__version__" for target in statement.targets):
            continue
        value = ast.literal_eval(statement.value)
        if isinstance(value, str) and value:
            return value
    raise RuntimeError(f"No string __version__ assignment found in {version_file}")


def find_sqlite_vec_dylib() -> Path:
    """Locate the sqlite-vec native extension for bundling."""
    vec_dir = VENV_SITE / "sqlite_vec"
    candidates = list(vec_dir.glob("vec0.*"))
    if not candidates:
        raise FileNotFoundError(f"Cannot find vec0.* in {vec_dir}")
    return candidates[0]


# The macOS .iconset naming contract (`man iconutil`): a valid iconset
# folder holds exactly these ten PNGs, and each file's pixel dimensions
# must equal the size encoded in its name. `@2x` is the Retina variant at
# double the pixel dimension (e.g. `icon_128x128@2x.png` is 256x256). The
# only legal base sizes are 16, 32, 128, 256 and 512 — a file with any
# other name (e.g. `icon_64x64.png`) makes `iconutil` reject the whole
# folder with "Invalid Iconset", and omitting the `@2x` entries ships an
# .icns with no Retina representation at that size.
ICONSET_ENTRIES: tuple[tuple[str, int], ...] = (
    ("icon_16x16.png", 16),
    ("icon_16x16@2x.png", 32),
    ("icon_32x32.png", 32),
    ("icon_32x32@2x.png", 64),
    ("icon_128x128.png", 128),
    ("icon_128x128@2x.png", 256),
    ("icon_256x256.png", 256),
    ("icon_256x256@2x.png", 512),
    ("icon_512x512.png", 512),
    ("icon_512x512@2x.png", 1024),
)


def _build_icns(svg_path: Path, out_icns: Path) -> Path:
    """Convert ``assets/icon.svg`` to a self-contained PNG-backed ICNS.

    Stage 7 (VECTORDRIVE_IMPLEMENTATION_PLAN.md): the packaged app has
    shipped with PyInstaller's generic default icon (icon=None) since
    v0.1 — this rasterizes the product's own SVG mark at every size
    macOS's .iconset format expects (see ``ICONSET_ENTRIES``), so the
    built .app finally carries a real icon.
    """
    import shutil
    import struct

    import pymupdf

    iconset = out_icns.parent / "icon.iconset"
    if iconset.exists():
        # Never merge with a stale partial run — a leftover file with the
        # wrong size, or a name macOS doesn't recognize, can invalidate the
        # resulting icon resource.
        shutil.rmtree(iconset)
    iconset.mkdir(parents=True)

    doc = pymupdf.open(svg_path)
    page = doc[0]
    if round(page.rect.width) != round(page.rect.height):
        raise ValueError(
            f"{svg_path} is not square ({page.rect.width}x{page.rect.height}); "
            "a square source is required for a macOS iconset"
        )

    for name, px in ICONSET_ENTRIES:
        scale = px / page.rect.width
        pix = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=True)
        if (pix.width, pix.height) != (px, px):
            raise ValueError(
                f"rasterized {name} at {pix.width}x{pix.height}, expected {px}x{px}"
            )
        pix.save(iconset / name)

    # Write the modern PNG-backed ICNS container directly. Newer macOS SDK
    # versions have rejected otherwise valid iconutil input from generated
    # PNGs, which made clean builds depend accidentally on a stale .icns.
    # These are Apple's standard physical-size chunk types.
    chunks = []
    for chunk_type, filename in (
        (b"icp4", "icon_16x16.png"),
        (b"icp5", "icon_32x32.png"),
        (b"icp6", "icon_32x32@2x.png"),
        (b"ic07", "icon_128x128.png"),
        (b"ic08", "icon_256x256.png"),
        (b"ic09", "icon_512x512.png"),
        (b"ic10", "icon_512x512@2x.png"),
    ):
        payload = (iconset / filename).read_bytes()
        chunks.append(chunk_type + struct.pack(">I", len(payload) + 8) + payload)
    body = b"".join(chunks)
    out_icns.write_bytes(b"icns" + struct.pack(">I", len(body) + 8) + body)
    if not out_icns.exists() or out_icns.stat().st_size == 0:
        raise RuntimeError(f"ICNS generation produced no file at {out_icns}")
    return out_icns


def _stamp_bundle_version(app_path: Path, version: str) -> None:
    """Write the authoritative version into the generated Info.plist.

    PyInstaller defaults the Finder-visible version to ``0.0.0``. About,
    the CLI, and wheel metadata already read ``vectordrive.__version__``;
    stamping the bundle here keeps macOS on that same source.
    """
    import plistlib

    info_plist = app_path / "Contents" / "Info.plist"
    if not info_plist.exists():
        raise FileNotFoundError(f"Built app is missing {info_plist}")
    with info_plist.open("rb") as handle:
        info = plistlib.load(handle)
    info["CFBundleShortVersionString"] = version
    info["CFBundleVersion"] = version
    with info_plist.open("wb") as handle:
        plistlib.dump(info, handle, sort_keys=False)
    print(f"  Stamped bundle version: {version}")


def _fix_bundled_openssl_conflict(app_path: Path) -> None:
    """Replace PyInstaller's shared Contents/Frameworks/lib{crypto,ssl}.3.dylib
    with Homebrew's openssl@3 build, then re-sign.

    PyInstaller collects native deps into one shared Contents/Frameworks/
    directory keyed by basename. Both Python's own `_ssl` extension and
    cv2 (rapidocr's dependency, needed for tier2 OCR — see the
    --collect-all rapidocr/onnxruntime comment above) require a file named
    libcrypto.3.dylib/libssl.3.dylib, and only one physical copy survives:
    empirically (verified against a real build) it's cv2's vendored
    OpenSSL, which is missing symbols `_ssl` needs (e.g.
    X509_STORE_get1_objects) — this crashes `_ssl` on import, which
    api.py's `import webview` (via bottle -> ssl) then reports as the
    unrelated-looking "GUI requires the 'gui' extra" error. Homebrew's
    openssl@3 has the missing symbols and is API-compatible with cv2's
    (basic TLS calls only, via ffmpeg's libavformat) — verified by the
    packaging test suite passing (OCR + GUI launch) after this swap.

    Requires Homebrew's openssl@3 to be installed on the *build* machine
    only — the swapped-in dylib's install name is rewritten to @rpath so
    the packaged app itself stays self-contained (requires nothing extra
    on the machine that runs VectorDrive.app).
    """
    import shutil
    import subprocess

    frameworks = app_path / "Contents" / "Frameworks"
    try:
        brew_prefix = Path(
            subprocess.check_output(["brew", "--prefix", "openssl@3"], text=True).strip()
        )
    except (OSError, subprocess.CalledProcessError):
        brew_prefix = Path("/opt/homebrew/opt/openssl@3")
    src_dir = brew_prefix / "lib"

    for name in ("libcrypto.3.dylib", "libssl.3.dylib"):
        src = src_dir / name
        dest = frameworks / name
        if not src.exists():
            raise FileNotFoundError(
                f"Homebrew openssl@3 not found at {src} — install it with "
                f"`brew install openssl@3` to build VectorDrive.app"
            )
        dest.unlink(missing_ok=True)  # was a symlink into cv2's .dylibs/
        shutil.copy2(src, dest)
        dest.chmod(0o755)
        subprocess.run(
            ["install_name_tool", "-id", f"@rpath/{name}", str(dest)], check=True
        )

    # Homebrew's libssl.3.dylib links libcrypto by absolute Cellar path —
    # repoint it at the bundled @rpath copy so the app doesn't need
    # Homebrew present at runtime.
    libssl = frameworks / "libssl.3.dylib"
    old_crypto_ref = subprocess.check_output(
        ["otool", "-L", str(libssl)], text=True
    )
    for line in old_crypto_ref.splitlines():
        line = line.strip()
        if line.startswith(str(src_dir)) and "libcrypto" in line:
            old_path = line.split(" (")[0]
            subprocess.run(
                ["install_name_tool", "-change", old_path, "@rpath/libcrypto.3.dylib", str(libssl)],
                check=True,
            )
            break

    print("  Fixed openssl conflict: swapped in Homebrew's libcrypto/libssl for _ssl compatibility")

    # Re-sign: the binary swap above invalidates PyInstaller's ad-hoc signature.
    subprocess.run(["codesign", "--force", "--deep", "--sign", "-", str(app_path)], check=True)
    print("  Re-signed app bundle (ad-hoc)")


def build():
    import PyInstaller.__main__

    web_dir = PROJECT_ROOT / "src" / "vectordrive" / "gui" / "web"
    schema_sql = PROJECT_ROOT / "src" / "vectordrive" / "db" / "schema.sql"
    sqlite_vec_dylib = find_sqlite_vec_dylib()

    datas = [
        (str(web_dir), "vectordrive/gui/web"),
        (str(schema_sql), "vectordrive/db"),
        (str(sqlite_vec_dylib), "sqlite_vec"),
    ]

    hidden_imports = [
        "vectordrive",
        "vectordrive.gui",
        "vectordrive.gui.app",
        "vectordrive.gui.api",
        "vectordrive.gui.jobs",
        "vectordrive.gui.events",
        "vectordrive.gui.state",
        "vectordrive.services",
        "vectordrive.services.claude_desktop",
        "vectordrive.services.mcp_probe",
        "vectordrive.services.settings_service",
        "vectordrive.services.health_service",
        "vectordrive.services.index_service",
        "vectordrive.services.folders_service",
        "vectordrive.services.status_service",
        "vectordrive.services.engines",
        "vectordrive.services.atomic_io",
        "vectordrive.services.errors",
        "vectordrive.services.locking",
        "vectordrive.services.platform_paths",
        "vectordrive.config",
        "vectordrive.db",
        "vectordrive.db.connection",
        "vectordrive.db.readonly",
        "vectordrive.mcp",
        "vectordrive.mcp.server",
        "vectordrive.mcp.tools",
        "vectordrive.mcp.context",
        "vectordrive.mcp.readonly_db",
        "vectordrive.mcp.path_safety",
        "vectordrive.cli",
        "vectordrive.cli.main",
        "vectordrive.pipeline",
        "vectordrive.search",
        "sqlite_vec",
        "webview",
        "webview.platforms",
        "webview.platforms.cocoa",
        "PyObjCTools",
        "PyObjCTools.AppHelper",
        "bottle",
        "proxy_tools",
    ]

    args = [
        str(PROJECT_ROOT / "src" / "vectordrive" / "gui" / "app.py"),
        "--name", "VectorDrive",
        "--windowed",
        "--onedir",
        "--noconfirm",
        "--distpath", str(PROJECT_ROOT / "dist"),
        "--workpath", str(PROJECT_ROOT / "build" / "pyinstaller"),
        "--specpath", str(PROJECT_ROOT),
    ]

    for src, dest in datas:
        args.extend(["--add-data", f"{src}{os.pathsep}{dest}"])

    for hi in hidden_imports:
        args.extend(["--hidden-import", hi])

    # Add the src directory to the path so imports resolve
    args.extend(["--paths", str(PROJECT_ROOT / "src")])

    # Collect all webview files (including platforms, JS, and all deps)
    args.extend(["--collect-all", "webview"])
    args.extend(["--collect-all", "bottle"])
    args.extend(["--collect-all", "proxy_tools"])

    # Tier 2 OCR (RapidOCR, onnxruntime backend) is unconditionally constructed
    # by services/engines.py's build_index_engines() for every indexing run
    # (config.ocr has no toggle for it) — it must be bundled, not excluded, or
    # every OCR page silently fails in the packaged app. --collect-all rapidocr
    # pulls in its bundled .onnx model files (site-packages/rapidocr/models/);
    # --collect-all onnxruntime pulls in its native runtime binaries. cv2 is
    # rapidocr's own declared dependency (opencv-python) and is bundled
    # automatically via PyInstaller's opencv hook once it isn't excluded below.
    args.extend(["--collect-all", "rapidocr"])
    args.extend(["--collect-all", "onnxruntime"])

    # G14 finding: PyInstaller's own build log (warn-VectorDrive.txt)
    # flagged `pydantic.BaseModel` as a missing module — pydantic v2
    # exports it via a dynamic __getattr__ in pydantic/__init__.py that
    # PyInstaller's static analysis can't trace — cascading to every mcp
    # submodule that imports it (mcp.server.*, mcp.client.session,
    # mcp.client.stdio) and to vectordrive.mcp.tools itself, breaking the
    # packaged app's entire MCP server *and* client-side deep-probe path.
    # --collect-all forces inclusion regardless of what static analysis
    # could trace.
    args.extend(["--collect-all", "pydantic"])
    args.extend(["--collect-all", "mcp"])
    # mcp.cli is the SDK's own optional dev-CLI submodule (unused by
    # VectorDrive — we only use mcp.server/mcp.client) and requires
    # `typer`, not installed here; it calls sys.exit(1) at import time
    # when typer is missing, which crashes --collect-all's own submodule
    # probing (a plain ImportError would be tolerated; SystemExit isn't).
    args.extend(["--exclude-module", "mcp.cli"])

    # Exclude heavy ML deps for tier3 (PaddleOCR-VL) and tier4 (Tesseract) —
    # both default OFF (config.ocr.tier3_paddleocr_vl_enabled /
    # tier4_tesseract_enabled = False) and both lazily import their backend
    # only inside their engine's run method, so excluding them here never
    # breaks tier2/default OCR. A user who enables tier3 in a packaged app
    # gets a clear per-file OCR-failed status (see indexer.py's ocr_warning)
    # instead of a silent import crash — not full tier3 support, which would
    # require shipping paddlepaddle's ~1-2GB of weights.
    # NOTE: numpy is kept - needed by vectordrive.search.vector_index.numpy_index
    # NOTE: cv2/onnxruntime removed from this list (see above) - tier2 needs them.
    for exc in ["paddle", "paddleocr", "paddlex", "scipy", "sklearn", "torch", "torchvision"]:
        args.extend(["--exclude-module", exc])

    # macOS-specific: set bundle identifier, real app icon, and don't
    # request console.
    if sys.platform == "darwin":
        args.extend(["--osx-bundle-identifier", "com.vectordrive.app"])
        icon_svg = PROJECT_ROOT / "assets" / "icon.svg"
        if icon_svg.exists():
            icns_path = _build_icns(icon_svg, PROJECT_ROOT / "build" / "icon.icns")
            args.extend(["--icon", str(icns_path)])
            print(f"  icon: {icns_path}")
        else:
            print(f"  WARNING: {icon_svg} not found — building with no custom icon")

    print(f"Building VectorDrive.app...")
    print(f"  sqlite-vec dylib: {sqlite_vec_dylib}")
    print(f"  web dir: {web_dir}")
    PyInstaller.__main__.run(args)

    if sys.platform == "darwin":
        app_path = PROJECT_ROOT / "dist" / "VectorDrive.app"
        _stamp_bundle_version(app_path, _read_project_version())
        _fix_bundled_openssl_conflict(app_path)

    app_path = PROJECT_ROOT / "dist" / "VectorDrive.app"
    if sys.platform == "darwin" and app_path.exists():
        print(f"\n✓ Built: {app_path}")
        print(f"  Size: {sum(f.stat().st_size for f in app_path.rglob('*') if f.is_file()) / 1024 / 1024:.1f} MB")
    else:
        # Non-macOS or onedir mode: check for the binary
        binary = PROJECT_ROOT / "dist" / "VectorDrive" / "VectorDrive"
        if binary.exists():
            print(f"\n✓ Built: {binary}")
        else:
            print("\n✗ Build may have failed — check output above.")
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(build())
