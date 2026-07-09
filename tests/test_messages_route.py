"""Tests for the /messages admin route's media thumbnail rendering.

Pins the contract from openspec change `add-image-and-video-support`,
section 10.1:

- The Messages table renders a Media column for every row.
- An SMS-only message (media=[]) shows a dash placeholder.
- An MMS message with a non-empty media list renders one
  `<img>` (for `image/*` mime) or one play-badge `<a>` (for
  `video/*` mime) per attachment.
- The thumbnail's `src=` is the Flask proxy URL
  `/api/media/<key>` — never a signed S3 URL — so the browser
  call goes through the same auth/redirect path the Pi uses.

The test drives Flask in-process via the same harness shape as
`test_admin_settings_route.py`: heavy deps (sqlite, s3, MQTT,
Paho) are mocked so we exercise only the route + template layer.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_PROJECT_ROOT = Path(__file__).parent.parent
_MAIN_PATH = _PROJECT_ROOT / "heart-message-manager" / "main.py"


def _make_mock_cfg():
    """Minimal mock cfg so config_reader and friends have something to read."""
    cfg = MagicMock()
    cfg.MQTT_CLIENT = "paho"
    cfg.MQTT_HOST = "localhost"
    cfg.MQTT_PORT = 1883
    cfg.MQTT_USERNAME = "test"
    cfg.MQTT_PASSWORD = "test"
    cfg.MQTT_TOPIC = "test/feeds/sign"
    cfg.AWS_ACCESS_KEY_ID = "test"
    cfg.AWS_SECRET_ACCESS_KEY = "test"
    cfg.AWS_S3_BUCKET = "test"
    cfg.AWS_S3_REGION = "us-east-1"
    cfg.CONFIG_API_URL = "http://localhost/api/config"
    cfg.MESSAGES_API_URL = "http://localhost/api/messages"
    cfg.if_exists = MagicMock(
        side_effect=lambda k: {
            "ADMIN_USERNAME": "admin",
            "ADMIN_PASSWORD": "secret123",
            "API_SECRET_KEY": "esp32-api-key",
            "ADMIN_SESSION_TIMEOUT_MINS": "60",
            "TWILIO_AUTH_TOKEN": "twilio-auth-token",
        }.get(k)
    )
    return cfg


def _load_app_module(mock_cfg, paho_client_ctor):
    """Same harness as `test_admin_settings_route.py` — see that
    file's `_load_app_module` for the rationale on each mocked
    module.

    NB: `sqlite.get_all_messages` is a MagicMock so the test
    installs canned `Message` objects with the `media` field it
    wants to verify (replacing the mock's `return_value` is what
    individual tests do).
    """
    mock_modules = {}

    def _make_mock(name):
        mod = types.ModuleType(name)
        mock_modules[name] = mod
        sys.modules[name] = mod
        return mod

    lib_shared = _make_mock("lib_shared")
    lib_shared.__path__ = [str(_PROJECT_ROOT / "lib_shared")]
    config_reader_mod = _make_mock("lib_shared.config_reader")
    config_reader_mod.get_config = lambda required_keys=None: mock_cfg  # type: ignore[attr-defined]
    log_setup_mod = _make_mock("lib_shared.log_setup")
    log_setup_mod.configure_logging = MagicMock()  # type: ignore[attr-defined]

    models_mod = _make_mock("lib_shared.models")
    models_mod.SignConfig = MagicMock()  # type: ignore[attr-defined]
    models_mod.FilterRule = MagicMock()  # type: ignore[attr-defined]
    models_mod.Message = MagicMock()  # type: ignore[attr-defined]

    class _FakeEnvelope:
        def __init__(self, type, payload):
            self.type = type
            self.payload = payload

        def to_json(self):
            return json.dumps({"type": self.type, "payload": self.payload}, separators=(",", ":"))

    models_mod.MessageEnvelope = _FakeEnvelope  # type: ignore[attr-defined]
    models_mod.MessageView = MagicMock()  # type: ignore[attr-defined]

    cm_mod = _make_mock("lib_shared.config_migrations")
    cm_mod.migrate = MagicMock(side_effect=lambda d, current_version: d or {})  # type: ignore[attr-defined]
    cm_mod.migrate_on_startup = MagicMock()  # type: ignore[attr-defined]

    mm_mod = _make_mock("lib_shared.message_manager")
    mm_mod.MessageManager = MagicMock()  # type: ignore[attr-defined]

    paho_mod = _make_mock("lib_shared.paho_mqtt_client")
    paho_mod.PahoMqttClient = paho_client_ctor  # type: ignore[attr-defined]  # noqa: E501

    def _load_real_module(name, path):
        spec = importlib.util.spec_from_file_location(name, str(path))
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
        return mod

    auth_real_path = _PROJECT_ROOT / "heart-message-manager" / "auth.py"
    auth_mod = _load_real_module("heart-message-manager.auth", auth_real_path)
    sys.modules["auth"] = auth_mod

    _make_mock("heart-message-manager.sqlite")
    _make_mock("heart-message-manager.s3")
    _make_mock("heart-message-manager.server_time")
    _make_mock("heart-message-manager.paho_mqtt_client")

    sqlite_mod = types.ModuleType("sqlite")
    sqlite_mod.rebuild_from_s3 = MagicMock()  # type: ignore[attr-defined]
    sqlite_mod.get_config = MagicMock()  # type: ignore[attr-defined]
    sqlite_mod.get_all_messages = MagicMock(return_value=[])  # type: ignore[attr-defined]
    sqlite_mod.get_messages_since = MagicMock(return_value=[])  # type: ignore[attr-defined]
    sqlite_mod.message_count = MagicMock(return_value=0)  # type: ignore[attr-defined]
    sqlite_mod.put_message = MagicMock()  # type: ignore[attr-defined]
    sqlite_mod.get_message = MagicMock(return_value=None)  # type: ignore[attr-defined]
    sqlite_mod.put_config = MagicMock()  # type: ignore[attr-defined]
    sys.modules["sqlite"] = sqlite_mod

    s3_mod = types.ModuleType("s3")
    s3_mod.load_messages_from_s3 = MagicMock(return_value=[])  # type: ignore[attr-defined]
    s3_mod.load_latest_config = MagicMock(return_value=None)  # type: ignore[attr-defined]
    s3_mod.log_message = MagicMock()  # type: ignore[attr-defined]
    s3_mod.save_config_snapshot = MagicMock()  # type: ignore[attr-defined]
    s3_mod._s3_bucket = MagicMock(return_value="test-bucket")  # type: ignore[attr-defined]
    s3_mod._s3_client = MagicMock()  # type: ignore[attr-defined]
    sys.modules["s3"] = s3_mod

    server_time_mod = types.ModuleType("server_time")
    server_time_mod.format_from_iso = lambda *args, **kwargs: ""  # type: ignore[attr-defined]
    server_time_mod.now_utc_iso = lambda: "2026-05-22T00:00:00Z"  # type: ignore[attr-defined]
    sys.modules["server_time"] = server_time_mod

    paho_mm_mod = types.ModuleType("paho_mqtt_client")
    paho_mm_mod.PahoMqttClient = MagicMock()  # type: ignore[attr-defined]
    sys.modules["paho_mqtt_client"] = paho_mm_mod

    spec = importlib.util.spec_from_file_location("heart_message_manager_main", str(_MAIN_PATH))
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["heart-message-manager.main"] = mod
    spec.loader.exec_module(mod)

    flask_app = mod.app
    flask_app.jinja_loader = None
    from jinja2 import FileSystemLoader

    flask_app.jinja_env = flask_app.create_jinja_environment()
    flask_app.jinja_env.loader = FileSystemLoader(str(_PROJECT_ROOT / "heart-message-manager" / "templates"))

    return flask_app


# ---------------------------------------------------------------------------
# Fixtures (mirror test_admin_settings_route.py exactly)
# ---------------------------------------------------------------------------


@pytest.fixture
def app(monkeypatch):
    """Flask app + the captured PahoMqttClient constructor + the captured MQTT client instance."""
    captured = {}

    class _RecordingPaho:
        def __init__(self, dispatch_callback, **kwargs):
            captured["dispatch_callback"] = dispatch_callback
            captured["kwargs"] = kwargs
            self.publish_envelope = MagicMock(return_value=True)
            self.start = MagicMock()
            self.stop = MagicMock()
            captured["instance"] = self

    mock_cfg = _make_mock_cfg()

    # Capture the real lib_shared.* modules before `_load_app_module`
    # replaces them in `sys.modules` with MagicMocks. Without this
    # restore, downstream tests (e.g. tests/test_paho_mqtt_client.py,
    # which `from lib_shared.paho_mqtt_client import PahoMqttClient`)
    # would see the mocked module left behind and fail with
    # `'_RecordingPaho' object has no attribute '_host'` etc.
    real_modules: dict[str, object] = {}
    for name in list(sys.modules):
        if name == "lib_shared" or name.startswith("lib_shared."):
            real_modules[name] = sys.modules[name]

    monkeypatch.delenv("HEROKU_SLUG_COMMIT", raising=False)
    monkeypatch.delenv("EFFECTS_SETTINGS_OVERRIDE", raising=False)

    flask_app = _load_app_module(mock_cfg, _RecordingPaho)
    flask_app.config["TESTING"] = True
    flask_app.config["WTF_CSRF_ENABLED"] = False

    captured["flask_app"] = flask_app

    try:
        yield flask_app, captured
    finally:
        for name, real_mod in real_modules.items():
            sys.modules[name] = real_mod
        for name in list(sys.modules):
            if (name == "lib_shared" or name.startswith("lib_shared.")) and name not in real_modules:
                sys.modules.pop(name, None)


@pytest.fixture
def client(app):
    flask_app, _ = app
    return flask_app.test_client()


def _make_cfg():
    """A MagicMock cfg the `messages.html` template iterates."""
    cfg = MagicMock()
    cfg.sign.name = "Lindsay's Heart"
    cfg.timezone = "America/Los_Angeles"
    cfg.filters = []
    cfg.senders = {}
    return cfg


def _login(client):
    """Standard /login form post so the session is authenticated."""
    response = client.post("/login", data={"username": "admin", "password": "secret123"})
    assert response.status_code in (200, 302), response.data


def _make_message(message_id, body, media, sender="+15551234567", received_at="2026-07-09T00:00:00Z"):
    """Build a small `Message` duck-type the template iterates.

    `messages.html` reads `msg.id`, `msg.sender`, `msg.body`,
    `msg.received_at`, and `msg.media`. Using a SimpleNamespace
    rather than the real `Message` dataclass keeps the test
    isolated from any future change to Message's constructor
    signature.
    """
    from types import SimpleNamespace

    return SimpleNamespace(
        id=message_id,
        sender=sender,
        body=body,
        received_at=received_at,
        media=media,
    )


# ---------------------------------------------------------------------------
# Media column header + dash placeholder for SMS-only messages
# ---------------------------------------------------------------------------


def test_messages_page_renders_media_column_header(app, client):
    """The /messages admin page renders a "Media" column header even
    when there are no messages — the column is part of the SSR
    template, not a per-row conditional."""
    import sqlite as sqlite_mod

    sqlite_mod.get_config.return_value = _make_cfg()
    sqlite_mod.get_all_messages.return_value = []
    _login(client)

    response = client.get("/messages")
    assert response.status_code == 200, response.data
    body = response.get_data(as_text=True)
    assert ">Media</th>" in body or "Media</th>" in body


def test_messages_page_sms_only_row_shows_dash_placeholder(app, client):
    """An SMS-only message (media=[]) renders a dash placeholder in
    the Media column instead of any thumbnail elements. The
    placeholder is what the operator sees for rows that have no
    attachments — keeps the row layout consistent."""
    import sqlite as sqlite_mod

    sms_msg = _make_message("m1", "hello there", media=[])
    sqlite_mod.get_config.return_value = _make_cfg()
    sqlite_mod.get_all_messages.return_value = [sms_msg]
    _login(client)

    response = client.get("/messages")
    assert response.status_code == 200, response.data
    body = response.get_data(as_text=True)
    # The dash placeholder for empty media.
    assert "—" in body
    # No thumbnail elements were rendered for an SMS-only row.
    assert 'data-testid="media-thumb"' not in body
    assert 'data-testid="media-thumb-video"' not in body


# ---------------------------------------------------------------------------
# Image attachments: <img> rendered with Flask proxy URL
# ---------------------------------------------------------------------------


def test_messages_page_image_attachment_renders_img_with_proxy_url(app, client):
    """An MMS message with an image/* attachment renders an <img>
    whose `src` is the Flask /api/media/<key> proxy URL — never
    a signed S3 URL. Browser auth flows through Flask session
    cookies on the same-origin call."""
    import sqlite as sqlite_mod

    img_msg = _make_message(
        "m1",
        "look at this",
        media=[{"type": "image/jpeg", "url": "media/images/2026-07/a.jpg"}],
    )
    sqlite_mod.get_config.return_value = _make_cfg()
    sqlite_mod.get_all_messages.return_value = [img_msg]
    _login(client)

    response = client.get("/messages")
    assert response.status_code == 200, response.data
    body = response.get_data(as_text=True)
    # Thumbnail anchor rendered.
    assert 'data-testid="media-thumb"' in body
    # src is the Flask proxy URL, not a signed S3 URL.
    assert "/api/media/media/images/2026-07/a.jpg" in body
    # No signed-URL signature prefix leaked into the template
    # output — that would be a regression (the operator's browser
    # doesn't need to know about S3 signing).
    assert "X-Amz-Signature" not in body
    assert "X-Amz-Expires" not in body


# ---------------------------------------------------------------------------
# Video attachments: play-badge <a> with the same Flask proxy URL
# ---------------------------------------------------------------------------


def test_messages_page_video_attachment_renders_play_badge(app, client):
    """An MMS message with a video/* attachment renders a play-badge
    `<a>` (no inline `<video>` element — the messages list has 50+
    rows so we don't autoplay every clip on page load). Clicking
    the badge opens the same Flask /api/media/<key> URL the Pi
    fetches, which 302s to the signed S3 URL on demand."""
    import sqlite as sqlite_mod

    vid_msg = _make_message(
        "m2",
        "watch this",
        media=[{"type": "video/mp4", "url": "media/videos/2026-07/b.mp4"}],
    )
    sqlite_mod.get_config.return_value = _make_cfg()
    sqlite_mod.get_all_messages.return_value = [vid_msg]
    _login(client)

    response = client.get("/messages")
    assert response.status_code == 200, response.data
    body = response.get_data(as_text=True)
    # Video thumb rendered as a data-testid'd <a>.
    assert 'data-testid="media-thumb-video"' in body
    # Same Flask proxy URL shape as the image case.
    assert "/api/media/media/videos/2026-07/b.mp4" in body


def test_messages_page_mixed_media_renders_each_kind(app, client):
    """An MMS message with multiple attachments (image + video +
    a different mime) renders one element per attachment, in
    iteration order. The unknown-mime fallback uses a paperclip
    badge so the operator sees that something is attached even
    when the browser preview would not know how to render it."""
    import sqlite as sqlite_mod

    mixed_msg = _make_message(
        "m3",
        "mixed",
        media=[
            {"type": "image/png", "url": "media/images/2026-07/a.png"},
            {"type": "video/mp4", "url": "media/videos/2026-07/b.mp4"},
            {"type": "application/pdf", "url": "media/other/c.pdf"},
        ],
    )
    sqlite_mod.get_config.return_value = _make_cfg()
    sqlite_mod.get_all_messages.return_value = [mixed_msg]
    _login(client)

    response = client.get("/messages")
    assert response.status_code == 200, response.data
    body = response.get_data(as_text=True)
    # All three attachment types rendered (one each).
    assert 'data-testid="media-thumb"' in body
    assert 'data-testid="media-thumb-video"' in body
    assert 'data-testid="media-thumb-other"' in body
    # All three keys routed through the Flask proxy.
    assert "/api/media/media/images/2026-07/a.png" in body
    assert "/api/media/media/videos/2026-07/b.mp4" in body
    assert "/api/media/media/other/c.pdf" in body


# ---------------------------------------------------------------------------
# Lightbox modal: present on the page so image thumbs can open full-size
# ---------------------------------------------------------------------------


def test_messages_page_lightbox_present(app, client):
    """The lightbox modal element is present on every /messages
    render so the JS-side click handler on the image thumb has
    somewhere to open. Lazy-loaded by a small inline script that
    prefers `dataset.fullUrl` (defensive — falls back to the
    anchor's `href`). No styling beyond `hidden` to avoid SSR
    flashes; the JS adds `flex` on click."""
    import sqlite as sqlite_mod

    sqlite_mod.get_config.return_value = _make_cfg()
    sqlite_mod.get_all_messages.return_value = []
    _login(client)

    response = client.get("/messages")
    assert response.status_code == 200, response.data
    body = response.get_data(as_text=True)
    assert 'id="media-lightbox"' in body
    assert 'data-testid="media-lightbox"' in body


# ---------------------------------------------------------------------------
# Mixed: SMS-only and MMS rows render side-by-side without errors
# ---------------------------------------------------------------------------


def test_messages_page_mixed_sms_and_mms_rows(app, client):
    """A page with both SMS-only and MMS rows renders each row
    correctly — the SMS rows show the dash, the MMS rows show
    thumbnails. No cross-row leakage (the `media` lookup is
    per-iteration)."""
    import sqlite as sqlite_mod

    sms_msg = _make_message("m1", "sms one", media=[])
    sms_msg_2 = _make_message("m2", "sms two", media=[])
    mms_msg = _make_message(
        "m3",
        "mms one",
        media=[{"type": "image/jpeg", "url": "media/images/x.jpg"}],
    )
    sqlite_mod.get_config.return_value = _make_cfg()
    sqlite_mod.get_all_messages.return_value = [sms_msg, mms_msg, sms_msg_2]
    _login(client)

    response = client.get("/messages")
    assert response.status_code == 200, response.data
    body = response.get_data(as_text=True)
    # Both messages rendered (senders visible).
    assert "+15551234567" in body
    # The MMS row has one image thumb.
    assert 'data-testid="media-thumb"' in body
    assert "/api/media/media/images/x.jpg" in body
    # At least one dash placeholder for the SMS rows.
    assert "—" in body
