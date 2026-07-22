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


def _snapshot_sys_modules():
    """Return a snapshot of sys.modules so we can restore it after a
    test that calls `_load_app_module` (which installs fake ModuleType
    objects into sys.modules for `lib_shared.*` and a few entry-level
    modules). Without the restore, tests that run after this one in
    the same pytest process see the mocks — which break real-package
    imports in tests like test_message_manager and
    scroller_matrix_test.
    """
    return {name: mod for name, mod in sys.modules.items()}


def _restore_sys_modules(snapshot):
    """Restore sys.modules to the state captured by `_snapshot_sys_modules`.

    Modules that were in the snapshot keep their value; modules added
    during the test (mock modules, app module) are dropped.
    """
    for name in list(sys.modules):
        if name not in snapshot:
            sys.modules.pop(name, None)
    for name, mod in snapshot.items():
        sys.modules[name] = mod


class _AppLoader:
    """Context manager around `test_auth._load_app_module` that snapshots
    sys.modules on entry and restores it on exit, so the lib_shared mocks
    the loader installs don't leak into downstream tests.
    """

    def __init__(self, auth):
        self._auth = auth
        self._snapshot = None

    def __enter__(self):
        self._snapshot = _snapshot_sys_modules()
        app = self._auth._load_app_module(self._auth._make_mock_cfg())
        app.config["TESTING"] = True
        return app

    def __exit__(self, *exc):
        _restore_sys_modules(self._snapshot)
        return False


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
    template = (_PROJECT_ROOT / "heart-message-manager" / "templates" / "dashboard.html").read_text()
    assert 'id="sign-canvas"' in template
    assert "<canvas" in template


def test_preview_template_uses_responsive_image_rendering():
    """The canvas's image-rendering style is responsive to the JS draw path.

    The LED look comes from a JS-side dot-mask that clips each cell to a
    fuzzy circle (see commit 9f5efb3 "preview: render LEDs as fuzzy
    circles instead of hard squares"), so the CSS no longer needs
    `image-rendering: pixelated`. The canvas is the sign-canvas element
    and declares an image-rendering style (the current value is `auto`).
    """
    template = (_PROJECT_ROOT / "heart-message-manager" / "templates" / "dashboard.html").read_text()
    assert 'id="sign-canvas"' in template
    assert "image-rendering:" in template


def test_preview_template_layers_media_behind_canvas():
    """The image and video DOM elements sit BEHIND the canvas (z-index 0),
    and the canvas sits in front (z-index 1), so the scroller text drawn
    to the transparent canvas overlays the media instead of being
    hidden by it.

    Pin this invariant: the canvas's Pillow WebCanvas writes lit pixels
    opaque and gaps transparent, so when the BrowserMediaOverlay's
    `<img>` / `<video>` element fills the panel, the canvas's gaps
    let the image through while the canvas's lit pixels (text, any
    active LED effect) sit on top. If the z-index gets swapped —
    image back on z-index 2, canvas on default — the scroller text
    gets re-hidden by the image as soon as the overlay's opacity
    reaches 1.0 during the fade-in hold.
    """
    template = (_PROJECT_ROOT / "heart-message-manager" / "templates" / "dashboard.html").read_text()
    assert 'style="z-index: 0; opacity: 0;"' in template, (
        "Image and video must be at z-index 0 (behind canvas) so the "
        "transparent canvas can composite the scroller text on top of "
        "the media rather than being hidden by it"
    )
    assert "z-index: 1" in template, (
        "Canvas must declare z-index 1 (above image/video at z-index 0) "
        "so its lit pixels sit on top of the media DOM element"
    )


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
    template = (_PROJECT_ROOT / "heart-message-manager" / "templates" / "dashboard.html").read_text()
    # The dark div (the preview frame) must be width-constrained so it
    # shrinks with the card. `w-full max-w-full` is the Tailwind for
    # `width: 100%; max-width: 100%`.
    assert "w-full max-w-full" in template, (
        "The dark div (preview frame) must be w-full max-w-full so it " "shrinks with the card on viewport resize"
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


def test_preview_template_media_overlay_matches_canvas_footprint():
    """The <img>/<video> media overlays MUST occupy the same square box as
    the canvas — NOT stretch-fill the dark div with `object-cover`, and
    NOT collapse to a different size than the canvas on wide viewports.

    Regression history:

    1. The original sizing (`absolute inset-0 w-full h-full object-cover`)
       filled the dark div with no width cap; on wide viewports the image
       blew past 800px wide while the canvas (capped at 800px) stayed
       behind it.

    2. The first attempt to fix this put `absolute inset-0 ... max-w +
       max-h + aspect-square` directly on the overlay. That collapsed to
       the smaller of the two maxes (the dark div's smaller axis) on a
       wide-but-short parent — the image shrank while the canvas hit the
       800px cap. Image and canvas were again at different sizes.

    The current fix wraps the canvas and the media overlays in a single
    sizing div (`relative w-full max-w-[min(800px,100%)] h-auto
    aspect-square mx-auto`). Both children fill that wrapper identically
    (`absolute inset-0 w-full h-full`), so the overlay footprint IS the
    canvas footprint by construction. `object-contain` preserves the
    source image's aspect ratio inside the square.

    Lit LEDs still paint on top (canvas z-index 1 > media z-index 0);
    what changed is only the media's footprint.
    """
    template = (_PROJECT_ROOT / "heart-message-manager" / "templates" / "dashboard.html").read_text()

    # The image overlay must fill its parent wrapper (which owns the
    # 800px cap + aspect-square). `absolute inset-0 w-full h-full` is
    # the "fill the parent box exactly" pattern; combined with the
    # wrapper's aspect-square, that produces a square footprint
    # identical to the canvas.
    img_block = _extract_tag(template, "browser-media-image")
    assert "absolute inset-0" in img_block, (
        "browser-media-image must use `absolute inset-0` to fill its "
        "sizing wrapper (which owns the 800px cap + aspect-square)"
    )
    assert "w-full h-full" in img_block, (
        "browser-media-image must declare `w-full h-full` so it fills " "the wrapper's box exactly (not just inset-0)"
    )

    # The image overlay must use `object-contain` (not `object-cover`)
    # so the source aspect ratio is preserved — wide landscape images
    # are letterboxed top/bottom, tall portrait images letterboxed
    # left/right, instead of being cropped to fit.
    assert "object-contain" in img_block, (
        "browser-media-image must use object-contain to preserve the "
        "source aspect ratio — object-cover crops the image to fit the "
        "overlay box"
    )
    # `object-cover` MUST NOT appear on the image overlay anymore —
    # that was the stretch-to-fill behavior being replaced.
    assert "object-cover" not in img_block, (
        "browser-media-image must NOT use object-cover — that's the " "stretch-to-fill behavior being replaced"
    )

    # Same constraints apply to the video overlay — videos also need
    # to fill the canvas footprint and preserve aspect ratio.
    video_block = _extract_tag(template, "browser-media-video")
    assert "absolute inset-0" in video_block
    assert "w-full h-full" in video_block
    assert "object-contain" in video_block
    assert "object-cover" not in video_block

    # The sizing wrapper must declare the cap + aspect-square so the
    # canvas and overlays end up at the same square footprint.
    assert "max-w-[min(800px,100%)]" in template, (
        "The sizing wrapper (or canvas) must cap at min(800px, 100%) — "
        "this is the rule that keeps the overlay and canvas at the same "
        "size"
    )
    assert "aspect-square" in template, (
        "The sizing wrapper (or canvas) must declare aspect-square so " "the overlay and canvas share a 1:1 footprint"
    )


def _extract_tag(template: str, element_id: str) -> str:
    """Return the substring of `template` covering the tag with `id=element_id`.

    Helper for the media-overlay tests so the assertions can target
    the specific element's class list instead of asserting on the
    whole template (which has two overlays + the canvas, each with
    their own sizing).
    """
    # The id is on the opening tag of an <img> or <video>. Grab from
    # the previous `<` to the closing `>` of that opening tag.
    needle = f'id="{element_id}"'
    start = template.find(needle)
    if start == -1:
        return ""
    open_lt = template.rfind("<", 0, start)
    close_gt = template.find(">", start)
    if open_lt == -1 or close_gt == -1:
        return ""
    return template[open_lt : close_gt + 1]


def test_preview_template_has_status_block():
    """The template shows the current effect name and message body."""
    template = (_PROJECT_ROOT / "heart-message-manager" / "templates" / "dashboard.html").read_text()
    assert 'id="preview-effect"' in template
    assert 'id="preview-message"' in template


def test_preview_template_has_loading_indicator():
    """A simulator-state indicator (#preview-loading) that hides once the
    runtime is running and shows the lifecycle state otherwise.

    Under #48 the canvas lives on `/` (dashboard), and the loading
    overlay's text reflects the active lifecycle state (Starting /
    Stopped / Error) rather than just "Loading preview…". The
    `id="preview-loading"` selector is the contract that ties
    `preview.js`'s `hideLoading()` to the DOM.
    """
    template = (_PROJECT_ROOT / "heart-message-manager" / "templates" / "dashboard.html").read_text()
    assert 'id="preview-loading"' in template


def test_preview_template_includes_pyscript_runtime():
    """The PyScript runtime script + py-config link are present."""
    template = (_PROJECT_ROOT / "heart-message-manager" / "templates" / "dashboard.html").read_text()
    assert "py-config" in template
    assert "py-script" in template
    assert "pyscript" in template.lower()


def test_preview_template_loads_preview_main():
    """The <py-script> tag points at the preview_main entry point."""
    template = (_PROJECT_ROOT / "heart-message-manager" / "templates" / "dashboard.html").read_text()
    assert "preview_main.py" in template


def test_preview_template_no_websocket():
    """v1 has no WebSocket — assert neither the JS nor template references one."""
    template = (_PROJECT_ROOT / "heart-message-manager" / "templates" / "dashboard.html").read_text()
    assert "new WebSocket" not in template
    assert "WebSocket(" not in template
    assert "Flask-Sock" not in template


def test_preview_template_loaded_by_app():
    """GET /preview follows a 302 to `/` which renders the dashboard with
    the canvas element (issue #48). `/preview` itself no longer renders
    the simulator document — the route is a back-compat redirect.
    """
    # Build the app via the same loader test_auth uses, so all heavy deps
    # are mocked but the template still renders through Jinja. The
    # _AppLoader wrapper snapshots and restores sys.modules so the
    # loader's lib_shared mocks don't leak into downstream tests.
    auth = _load_test_auth()
    with _AppLoader(auth) as app:
        client = app.test_client()
        # Need to log in first
        client.post("/login", data={"username": "admin", "password": "secret123"})
        response = client.get("/", follow_redirects=True)
        assert response.status_code == 200
        body = response.data.decode()
        assert 'id="sign-canvas"' in body
        # The CSS canvas no longer uses image-rendering: pixelated (commit
        # 9f5efb3 moved the LED look to a JS-side dot mask) — but the canvas
        # still declares an image-rendering style.
        assert "image-rendering:" in body
        # The simulator-state overlay now lives on `/` with id
        # `preview-loading`; the visible text reflects the lifecycle
        # state (Starting / Stopped / Error) rather than "Loading
        # preview…".
        assert 'id="preview-loading"' in body
        # No WebSocket references in the rendered HTML
        assert "new WebSocket" not in body
        assert "Flask-Sock" not in body


def test_preview_template_csp_header_on_preview_route():
    """The /preview response carries a Content-Security-Policy header that
    allows wasm-unsafe-eval and the PyScript CDN."""
    auth = _load_test_auth()
    with _AppLoader(auth) as app:
        client = app.test_client()
        client.post("/login", data={"username": "admin", "password": "secret123"})

        response = client.get("/")
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
    with _AppLoader(auth) as app:
        client = app.test_client()
        client.post("/login", data={"username": "admin", "password": "secret123"})

        response = client.get("/")
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
    """Non-dashboard routes are unaffected — no CSP is set.

    The dashboard CSP hook (`_set_dashboard_csp`) only fires on `/`
    and the legacy `/preview` (back-compat redirect). Routes outside
    the simulator must not carry the wasm-unsafe-eval / PyScript-CDN
    relaxations — they're irrelevant on /messages, /settings, etc.,
    and broadening them widens the attack surface for no reason.
    """
    auth = _load_test_auth()
    with _AppLoader(auth) as app:
        client = app.test_client()
        response = client.get("/health")
        assert response.headers.get("Content-Security-Policy", "") == ""


def test_preview_route_redirects_to_dashboard():
    """`/preview` is a 302 redirect to the dashboard `/` (issue #48 §4.5).

    The legacy `/preview` URL must not render a second simulator
    document — both runtime and operator confusion follow. The
    redirect's target (`url_for("dashboard")`) is the SSOT: a future
    route rename only needs to update one place.
    """
    auth = _load_test_auth()
    with _AppLoader(auth) as app:
        client = app.test_client()
        client.post("/login", data={"username": "admin", "password": "secret123"})
        # Don't follow the redirect — we want to assert it's a 302.
        response = client.get("/preview")
        assert response.status_code in (301, 302), (
            f"/preview must redirect (got {response.status_code}); "
            "issue #48 §4.6 requires a compatibility redirect to `/`."
        )
        location = response.headers.get("Location", "")
        # Flask's test client preserves the full path; just check it
        # points at the dashboard endpoint, not the legacy simulator.
        assert location.endswith("/"), (
            f"/preview must redirect to `/` (got Location={location!r})"
        )


def test_preview_csp_allows_s3_endpoint_when_explicit():
    """Regression: the /preview CSP `img-src` directive must allow the
    S3 origin so the Flask /api/media/<key> redirect can actually
    load image bytes in the browser.

    The /api/media/<key> route 302-redirects to a freshly-signed S3
    URL (presigned via boto3 in s3.py). The browser follows the
    redirect to the S3 origin and reads the image bytes directly —
    CSP `img-src` must allow that origin, otherwise the load is
    blocked with the "violates the following Content Security
    Policy directive: 'img-src 'self' data:''" error.

    The origin is derived from `AWS_S3_ENDPOINT_URL` (the dev
    MinIO path) or `AWS_S3_BUCKET + AWS_S3_REGION` (the prod AWS
    path). Pin both branches here.
    """
    from unittest.mock import MagicMock

    auth = _load_test_auth()

    def _cfg_with(extra_if_exists):
        """Build a mock cfg that returns the given if_exists entries
        on top of the standard admin/api keys."""
        cfg = auth._make_mock_cfg()
        defaults = {
            "ADMIN_USERNAME": "admin",
            "ADMIN_PASSWORD": "secret123",
            "API_SECRET_KEY": "esp32-api-key",
            "ADMIN_SESSION_TIMEOUT_MINS": "60",
            "TWILIO_AUTH_TOKEN": "twilio-auth-token",
        }
        merged = {**defaults, **extra_if_exists}
        cfg.if_exists = MagicMock(side_effect=lambda k: merged.get(k))
        return cfg

    # Dev: AWS_S3_ENDPOINT_URL is set (MinIO).
    dev_cfg = _cfg_with({"AWS_S3_ENDPOINT_URL": "http://localhost:9000"})
    snapshot = _snapshot_sys_modules()
    try:
        app = auth._load_app_module(dev_cfg)
        app.config["TESTING"] = True
        client = app.test_client()
        client.post("/login", data={"username": "admin", "password": "secret123"})
        response = client.get("/")
        csp = response.headers.get("Content-Security-Policy", "")
        img_src = _extract_directive(csp, "img-src")
        assert "http://localhost:9000" in img_src, f"img-src must allow the MinIO origin; got: {img_src!r}"
        # The same S3 origin must be in media-src — <video> and
        # <audio> elements fetch from the same signed-URL origin,
        # and without an explicit media-src they fall back to
        # default-src 'self' (which doesn't allow the S3 host).
        media_src = _extract_directive(csp, "media-src")
        assert (
            "http://localhost:9000" in media_src
        ), f"media-src must allow the MinIO origin (for <video>); got: {media_src!r}"
    finally:
        _restore_sys_modules(snapshot)

    # Prod: no AWS_S3_ENDPOINT_URL → virtual-hosted-style AWS URL.
    # us-east-1 is special-cased to the legacy global endpoint
    # (``<bucket>.s3.amazonaws.com``) — that's the actual signed URL
    # origin boto3 produces for us-east-1 buckets with virtual-host
    # addressing, so the CSP must allow that origin (not the
    # regional form ``<bucket>.s3.us-east-1.amazonaws.com``, which
    # boto3 never signs for this region).
    prod_cfg = _cfg_with(
        {"AWS_S3_BUCKET": "test-bucket", "AWS_S3_REGION": "us-east-1"},
    )
    snapshot = _snapshot_sys_modules()
    try:
        app = auth._load_app_module(prod_cfg)
        app.config["TESTING"] = True
        client = app.test_client()
        client.post("/login", data={"username": "admin", "password": "secret123"})
        response = client.get("/")
        csp = response.headers.get("Content-Security-Policy", "")
        img_src = _extract_directive(csp, "img-src")
        assert (
            "https://test-bucket.s3.amazonaws.com" in img_src
        ), f"img-src must allow the legacy AWS S3 origin (us-east-1); got: {img_src!r}"
        media_src = _extract_directive(csp, "media-src")
        assert (
            "https://test-bucket.s3.amazonaws.com" in media_src
        ), f"media-src must allow the legacy AWS S3 origin (for <video>); got: {media_src!r}"
    finally:
        _restore_sys_modules(snapshot)


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
    py_config_path = _PROJECT_ROOT / "heart-message-manager" / "static" / "preview" / "py-config.toml"
    cfg = tomllib.loads(py_config_path.read_text())
    declared = cfg.get("files", {})

    with _AppLoader(auth) as app:
        # main.py is loaded via importlib (see test_auth._load_app_module),
        # which causes Flask's root_path to fall back to the repo root and
        # static_folder to point at the wrong directory. Override explicitly,
        # mirroring what scripts/preview_server.py does for the real server.
        app.static_folder = str(_PROJECT_ROOT / "heart-message-manager" / "static")
        app.static_url_path = "/static"
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


def test_pyscript_files_declares_every_app_main_import():
    """Regression (issue #48): every Python module imported by
    `app_main.py` must be declared in `py-config.toml` `[files]`.
    PyScript's MEMFS only resolves modules that are listed in
    `[files]` — any undeclared import crashes the bootstrap with
    `ModuleNotFoundError` (browser console). The original
    dashboard-controller + dashboard-bootstrap modules shipped
    undeclared and the user saw the crash on every page load.

    Walks `app_main.py`'s top-level `from X import Y` / `import X`
    statements, normalizes each name to a py-config `[files]` key,
    and asserts the key is present. Skips `pyodide_js` (PyScript-
    provided), stdlib modules, and `js` / `window` (Pyodide proxies).
    """
    import ast
    import tomllib

    app_main_path = (
        _PROJECT_ROOT
        / "heart-message-manager"
        / "app_main.py"
    )
    py_config_path = (
        _PROJECT_ROOT
        / "heart-message-manager"
        / "static"
        / "preview"
        / "py-config.toml"
    )

    tree = ast.parse(app_main_path.read_text(encoding="utf-8"))
    declared = tomllib.loads(py_config_path.read_text()).get("files", {})

    # The `[files]` keys are full py-config paths like
    # `"heart-message-manager/dashboard_controller.py"`. We accept a
    # bare import name as "declared" if any declared key ends with
    # `/{name}.py` — that matches the file naming convention the
    # project uses (each module is a single file with a matching
    # name).
    declared_modules = {
        key.split("/")[-1].removesuffix(".py")
        for key in declared
        if key.endswith(".py")
    }

    # Modules PyScript / Pyodide provides at runtime, plus the
    # CPython stdlib (Pyodide ships with the whole stdlib by default).
    # Any of these can be `import`ed without a [files] entry.
    STDLIB_MODULES = frozenset(sys.stdlib_module_names) if hasattr(sys, "stdlib_module_names") else frozenset()
    RUNTIME_PROVIDED = {
        "pyodide_js",
        "pyodide",
        "js",
        "pyscript",
    } | STDLIB_MODULES

    missing: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".")[0]
                if top in RUNTIME_PROVIDED:
                    continue
                if top not in declared_modules:
                    missing.append(top)
        elif isinstance(node, ast.ImportFrom):
            if node.module is None or node.level > 0:
                continue  # relative import — already in same dir as app_main.py
            top = node.module.split(".")[0]
            if top in RUNTIME_PROVIDED:
                continue
            if top not in declared_modules:
                missing.append(top)

    assert not missing, (
        f"app_main.py imports {len(missing)} module(s) not declared in "
        f"py-config.toml [files]: {missing!r}. PyScript's MEMFS will "
        f"fail to resolve them at page-load and the dashboard will "
        f"throw ModuleNotFoundError on bootstrap. Add each as a "
        f"[files] entry mapping the source path to its "
        f"/static/preview/ URL."
    )
