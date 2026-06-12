"""Tests for the preview template's structure (canvas, status block, py-script tag).

Asserts the rendered /preview page contains the elements the design.md
proposed and does NOT contain any WebSocket/SSE infrastructure (v1 is
polling only).
"""

import importlib.util
import sys
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))


@pytest.fixture(autouse=True)
def _restore_lib_shared():
    """test_auth.py replaces sys.modules['lib_shared'] with a Mock. Re-import
    the real package before each test.
    """
    for mod_name in list(sys.modules):
        if mod_name == "lib_shared" or mod_name.startswith("lib_shared."):
            del sys.modules[mod_name]
    importlib.import_module("lib_shared")
    importlib.import_module("lib_shared.config_reader")
    yield


def _load_test_auth():
    """Import test_auth.py as a module (it doesn't auto-import on sys.path)."""
    auth_path = _PROJECT_ROOT / "tests" / "test_auth.py"
    spec = importlib.util.spec_from_file_location("tests.test_auth", str(auth_path))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["tests.test_auth"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_preview_template_has_sign_canvas_element():
    """The preview template renders a <canvas id='sign-canvas'> element."""
    template = (
        _PROJECT_ROOT / "heart-message-manager" / "templates" / "preview.html"
    ).read_text()
    assert 'id="sign-canvas"' in template
    assert "<canvas" in template


def test_preview_template_uses_pixelated_image_rendering():
    """The canvas element declares image-rendering: pixelated (LED look)."""
    template = (
        _PROJECT_ROOT / "heart-message-manager" / "templates" / "preview.html"
    ).read_text()
    assert "pixelated" in template


def test_preview_template_canvas_is_responsive():
    """The canvas is CSS-responsive so it scales with the viewport on resize.

    Regression: the previous version had a fixed inline `width: 512px;
    height: 512px;` on the canvas, so the dark div (bg-slate-900) sized
    to the canvas and didn't shrink when the viewport narrowed — the
    user saw a fixed-size preview frame regardless of window width.

    Fix: the dark div is `w-full max-w-full` (constrained to the card's
    content area) and the canvas uses `max-w-[min(800px,100%)]
    h-auto aspect-square` so it fills the dark div, caps at 800px, and
    stays square via aspect-ratio. The inline pixel width/height were
    removed.
    """
    template = (
        _PROJECT_ROOT / "heart-message-manager" / "templates" / "preview.html"
    ).read_text()
    # The dark div (the preview frame) must be width-constrained so it
    # shrinks with the card. `w-full max-w-full` is the Tailwind for
    # `width: 100%; max-width: 100%`.
    assert "w-full max-w-full" in template, (
        "The dark div (preview frame) must be w-full max-w-full so it "
        "shrinks with the card on viewport resize"
    )
    # The canvas must declare max-w-[min(800px,100%)] so the inline JS-set
    # width is clamped to both 800px and 100% of the dark div.
    assert "max-w-[min(800px,100%)]" in template, (
        "The canvas must cap at min(800px, 100% of dark div) via "
        "max-w-[min(800px,100%)] so it can't overflow the dark div"
    )
    # The canvas must declare aspect-square so the height tracks the
    # (possibly constrained) width — without this the canvas would be
    # a non-square rectangle if the JS-set width is clamped by max-w.
    assert "aspect-square" in template, (
        "The canvas must declare aspect-square so its height tracks the "
        "width and it stays square even when the width is constrained"
    )
    # The inline pixel width/height must NOT be present anymore.
    assert "width: 512px" not in template, (
        "The inline `width: 512px;` on the canvas was removed in favor "
        "of CSS-driven responsive sizing; its presence means the old "
        "fixed-size behavior is back"
    )
    assert "height: 512px" not in template


def test_preview_template_has_status_block():
    """The template shows the current effect name and message body."""
    template = (
        _PROJECT_ROOT / "heart-message-manager" / "templates" / "preview.html"
    ).read_text()
    assert 'id="preview-effect"' in template
    assert 'id="preview-message"' in template


def test_preview_template_has_loading_indicator():
    """A 'Loading preview…' indicator that hides once PyScript is ready."""
    template = (
        _PROJECT_ROOT / "heart-message-manager" / "templates" / "preview.html"
    ).read_text()
    assert "Loading preview" in template
    assert 'id="preview-loading"' in template


def test_preview_template_includes_pyscript_runtime():
    """The PyScript runtime script + py-config link are present."""
    template = (
        _PROJECT_ROOT / "heart-message-manager" / "templates" / "preview.html"
    ).read_text()
    assert "py-config" in template
    assert "py-script" in template
    assert "pyscript" in template.lower()


def test_preview_template_loads_preview_main():
    """The <py-script> tag points at the preview_main entry point."""
    template = (
        _PROJECT_ROOT / "heart-message-manager" / "templates" / "preview.html"
    ).read_text()
    assert "preview_main.py" in template


def test_preview_template_no_websocket():
    """v1 has no WebSocket — assert neither the JS nor template references one."""
    template = (
        _PROJECT_ROOT / "heart-message-manager" / "templates" / "preview.html"
    ).read_text()
    assert "new WebSocket" not in template
    assert "WebSocket(" not in template
    assert "Flask-Sock" not in template


def test_preview_template_loaded_by_app():
    """GET /preview returns a 200 with the rendered canvas element."""
    # Build the app via the same loader test_auth uses, so all heavy deps
    # are mocked but the template still renders through Jinja.
    auth = _load_test_auth()
    _make_mock_cfg = auth._make_mock_cfg
    _load_app_module = auth._load_app_module

    app = _load_app_module(_make_mock_cfg())
    app.config["TESTING"] = True
    client = app.test_client()
    # Need to log in first
    client.post("/login", data={"username": "admin", "password": "secret123"})
    response = client.get("/preview")
    assert response.status_code == 200
    body = response.data.decode()
    assert 'id="sign-canvas"' in body
    assert "image-rendering: pixelated" in body
    assert "Loading preview" in body
    # No WebSocket references in the rendered HTML
    assert "new WebSocket" not in body
    assert "Flask-Sock" not in body


def test_preview_template_csp_header_on_preview_route():
    """The /preview response carries a Content-Security-Policy header that
    allows wasm-unsafe-eval and the PyScript CDN."""
    auth = _load_test_auth()
    _make_mock_cfg = auth._make_mock_cfg
    _load_app_module = auth._load_app_module

    app = _load_app_module(_make_mock_cfg())
    app.config["TESTING"] = True
    client = app.test_client()
    client.post("/login", data={"username": "admin", "password": "secret123"})

    response = client.get("/preview")
    csp = response.headers.get("Content-Security-Policy", "")
    assert "wasm-unsafe-eval" in csp
    # PyScript CDN allowed in script-src
    assert "pyscript.net" in csp or "cdn.jsdelivr.net" in csp


def test_preview_template_csp_allows_tailwind_cdn():
    """Regression: /preview's CSP must allow the Tailwind play CDN and
    inline scripts. base.html loads Tailwind from cdn.tailwindcss.com and
    defines the theme via an inline `tailwind.config = {...}` block; if
    either is blocked by CSP, the flex sidebar layout collapses and the
    SVG nav icons render at full default size, making the page look like
    a stack of huge static icons.
    """
    auth = _load_test_auth()
    _make_mock_cfg = auth._make_mock_cfg
    _load_app_module = auth._load_app_module

    app = _load_app_module(_make_mock_cfg())
    app.config["TESTING"] = True
    client = app.test_client()
    client.post("/login", data={"username": "admin", "password": "secret123"})

    response = client.get("/preview")
    csp = response.headers.get("Content-Security-Policy", "")
    # The script-src directive must include the Tailwind CDN
    script_src = _extract_directive(csp, "script-src")
    assert (
        "https://cdn.tailwindcss.com" in script_src
    ), f"script-src must allow cdn.tailwindcss.com; got: {script_src!r}"
    # And the inline `tailwind.config = {...}` block in base.html
    assert "'unsafe-inline'" in script_src or "'unsafe-inline'" in csp, (
        f"script-src must allow inline scripts (for the tailwind.config "
        f"block in base.html); got: {script_src!r}"
    )


def _extract_directive(csp: str, name: str) -> str:
    """Return the substring of `csp` for the named directive (e.g. 'script-src').

    CSP directives are separated by ';'. We return everything from the named
    directive up to the next ';' so callers can assert on individual sources.
    """
    parts = [p.strip() for p in csp.split(";")]
    for p in parts:
        if p.startswith(name + " ") or p == name:
            return p
    return ""


def test_other_routes_have_no_csp_header():
    """Non-/preview routes are unaffected — no CSP is set."""
    auth = _load_test_auth()
    _make_mock_cfg = auth._make_mock_cfg
    _load_app_module = auth._load_app_module

    app = _load_app_module(_make_mock_cfg())
    app.config["TESTING"] = True
    client = app.test_client()
    response = client.get("/health")
    assert response.headers.get("Content-Security-Policy", "") == ""


def test_pyscript_declared_files_all_serve_200():
    """Regression: every file declared in py-config.toml [files] must be
    fetchable as a /static/preview/... URL.

    The static/preview/ tree uses symlinks to expose the Python source
    files to the browser. If any of those symlinks points to a path that
    doesn't exist, PyScript silently fails to load that module and
    preview_main.py never gets the chance to expose its globals —
    leaving the user staring at "Loading preview…" forever (since
    pyodideReady is never fired).

    This test walks the [files] section of py-config.toml and asserts
    that Flask serves each declared path with 200. It would have caught
    the original symlink-resolution bug (../../preview_*.py from
    static/preview/heart-message-manager/ instead of ../../../).
    """
    import tomllib

    auth = _load_test_auth()
    _make_mock_cfg = auth._make_mock_cfg
    _load_app_module = auth._load_app_module

    py_config_path = (
        _PROJECT_ROOT
        / "heart-message-manager"
        / "static"
        / "preview"
        / "py-config.toml"
    )
    cfg = tomllib.loads(py_config_path.read_text())
    declared = cfg.get("files", {})

    app = _load_app_module(_make_mock_cfg())
    # main.py is loaded via importlib (see test_auth._load_app_module),
    # which causes Flask's root_path to fall back to the repo root and
    # static_folder to point at the wrong directory. Override explicitly,
    # mirroring what scripts/preview_server.py does for the real server.
    app.static_folder = str(_PROJECT_ROOT / "heart-message-manager" / "static")
    app.static_url_path = "/static"
    app.config["TESTING"] = True
    client = app.test_client()

    failures = []
    # The keys in py-config.toml [files] are py-source-paths; the values
    # are /static/... URLs. We just want to assert each value returns 200.
    for src_path, static_url in declared.items():
        path = static_url.split("://", 1)[-1]  # strip scheme if any
        response = client.get(path)
        if response.status_code != 200:
            failures.append((src_path, path, response.status_code))

    assert not failures, (
        f"{len(failures)} py-config.toml [files] entries do not serve 200; "
        f"PyScript bootstrap will silently fail. First few: {failures[:3]}"
    )
