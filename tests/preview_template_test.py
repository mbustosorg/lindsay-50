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
