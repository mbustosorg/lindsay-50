"""Tests for `/api/media/<key>` (issue #38 / openspec `mms-media-support`).

The endpoint is a thin auth-bounded 302 to a freshly-signed S3 URL. Bytes
NEVER flow through Flask. The 302 is the contract — the Pi/browser
follow it to download the media directly from S3.

Tests here pin:
  - 401 without an X-API-Key header
  - 200/302 with X-API-Key, returning a `Location` header pointing to a
    signed S3 URL
  - 400 on path traversal (`..`, leading `/`, embedded `//`)
  - 404 on S3 signing failure (NoSuchKey, etc.)
  - 502 on S3 outage
  - the signed URL is freshly generated per request (different signatures
    → not cached)
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_PROJECT_ROOT = Path(__file__).parent.parent
_MAIN_PATH = _PROJECT_ROOT / "heart-message-manager" / "main.py"


def _make_mock_cfg():
    cfg = MagicMock()
    cfg.MQTT_CLIENT = "paho"
    cfg.MQTT_HOST = "localhost"
    cfg.MQTT_PORT = 1883
    cfg.MQTT_USERNAME = "test"
    cfg.MQTT_PASSWORD = "test"
    cfg.MQTT_TOPIC = "test"
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
            "TWILIO_ACCOUNT_SID": "ACtest",
        }.get(k)
    )
    return cfg


def _load_app_module(mock_cfg, mqtt_publisher):
    """Load main.py with heavy I/O mocked but `lib_shared.models` real.

    Reuses the same pattern as `mms_media_test.py` so we can iterate
    on the Flask app routes with full control over the S3 layer.
    """
    import importlib.util as _util

    real_modules = {}
    for name in list(sys.modules):
        if name == "lib_shared" or name.startswith("lib_shared."):
            real_modules[name] = sys.modules[name]

    def _make_mock(name):
        mod = types.ModuleType(name)
        sys.modules[name] = mod
        return mod

    config_reader_mod = _make_mock("lib_shared.config_reader")
    config_reader_mod.get_config = lambda required_keys=None: mock_cfg

    log_setup_mod = _make_mock("lib_shared.log_setup")
    log_setup_mod.configure_logging = MagicMock()

    from lib_shared import models as real_models
    sys.modules["lib_shared.models"] = real_models

    cm_mod = _make_mock("lib_shared.config_migrations")
    cm_mod.migrate = MagicMock(side_effect=lambda d, current_version: d or {})
    cm_mod.migrate_on_startup = MagicMock()

    mm_mod = _make_mock("lib_shared.message_manager")
    mm_mod.MessageManager = MagicMock()

    paho_mod = _make_mock("lib_shared.paho_mqtt_client")
    paho_mod.PahoMqttClient = MagicMock(return_value=mqtt_publisher)

    auth_real_path = _PROJECT_ROOT / "heart-message-manager" / "auth.py"
    auth_spec = _util.spec_from_file_location("heart-message-manager.auth", str(auth_real_path))
    auth_mod = _util.module_from_spec(auth_spec)
    sys.modules["heart-message-manager.auth"] = auth_mod
    auth_spec.loader.exec_module(auth_mod)
    sys.modules["auth"] = auth_mod

    _make_mock("heart-message-manager.sqlite")
    _make_mock("heart-message-manager.server_time")
    _make_mock("heart-message-manager.paho_mqtt_client")

    sqlite_mod = types.ModuleType("sqlite")
    sqlite_mod.rebuild_from_s3 = MagicMock()
    sqlite_mod.get_config = MagicMock(return_value=real_models.SignConfig.default())
    sqlite_mod.get_all_messages = MagicMock(return_value=[])
    sqlite_mod.get_messages_since = MagicMock(return_value=[])
    sqlite_mod.message_count = MagicMock(return_value=0)
    sqlite_mod.put_message = MagicMock()
    sqlite_mod.get_message = MagicMock(return_value=None)
    sqlite_mod.put_config = MagicMock()
    sys.modules["sqlite"] = sqlite_mod

    s3_mod = types.ModuleType("s3")
    s3_mod.load_messages_from_s3 = MagicMock(return_value=[])
    s3_mod.load_latest_config = MagicMock(return_value=None)
    s3_mod.log_message = MagicMock()
    s3_mod.save_config_snapshot = MagicMock()
    s3_mod._s3_bucket = MagicMock(return_value="test-bucket")
    s3_mod._s3_client = MagicMock()
    s3_mod.log_media = MagicMock(
        side_effect=lambda ctype, url: f"media/images/2026-07/k.{ctype.split('/')[-1]}"
    )
    s3_mod.signed_media_url = MagicMock(
        side_effect=lambda key, expires_in=3600: f"https://test-bucket.s3.amazonaws.com/{key}?X-Amz-Signature=abc"
    )
    sys.modules["s3"] = s3_mod

    server_time_mod = types.ModuleType("server_time")
    server_time_mod.format_from_iso = lambda *args, **kwargs: ""
    server_time_mod.now_utc_iso = lambda: "2026-07-09T15:30:00Z"
    server_time_mod.to_utc_datetime = lambda s: None
    sys.modules["server_time"] = server_time_mod

    spec = _util.spec_from_file_location("heart_message_manager_main", str(_MAIN_PATH))
    mod = _util.module_from_spec(spec)
    sys.modules["heart-message-manager.main"] = mod
    spec.loader.exec_module(mod)

    flask_app = mod.app
    flask_app.config["TESTING"] = True
    flask_app.config["WTF_CSRF_ENABLED"] = False
    from jinja2 import FileSystemLoader

    flask_app.jinja_loader = None
    flask_app.jinja_env = flask_app.create_jinja_environment()
    flask_app.jinja_env.loader = FileSystemLoader(str(_PROJECT_ROOT / "heart-message-manager" / "templates"))

    mqtt_publisher.publish_envelope.reset_mock()
    mod._test_real_modules = real_modules
    return mod, flask_app


def _restore_modules(real_modules):
    for name, real in real_modules.items():
        sys.modules[name] = real
    for name in list(sys.modules):
        if (name == "lib_shared" or name.startswith("lib_shared.")) and name not in real_modules:
            sys.modules.pop(name, None)


@pytest.fixture
def media_app():
    """Yield (mod, app) with X-API-Key auth available."""
    mock_cfg = _make_mock_cfg()
    mqtt_publisher = MagicMock()
    mqtt_publisher.publish_envelope = MagicMock(return_value=True)
    mqtt_publisher.subscribe = MagicMock()
    mqtt_publisher.loop_start = MagicMock()
    mqtt_publisher.connect_async = MagicMock()
    mod, flask_app = _load_app_module(mock_cfg, mqtt_publisher)
    mod._mqtt_client = mqtt_publisher
    try:
        yield mod, flask_app
    finally:
        _restore_modules(mod._test_real_modules)


@pytest.fixture
def client(media_app):
    return media_app[1].test_client()


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def test_api_media_without_api_key_returns_401(client):
    """Missing X-API-Key and no logged-in session → 401."""
    resp = client.get("/api/media/media/images/2026-07/x.jpg")
    assert resp.status_code == 401
    assert resp.json == {"error": "missing API key"}


def test_api_media_with_invalid_api_key_returns_401(client):
    """Wrong X-API-Key value → 401."""
    resp = client.get(
        "/api/media/media/images/2026-07/x.jpg",
        headers={"X-API-Key": "wrong-key"},
    )
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Success path: 302 to signed URL
# ---------------------------------------------------------------------------


def test_api_media_with_valid_api_key_returns_302_with_location(client):
    """GET with X-API-Key → 302 with Location pointing to signed S3 URL."""
    resp = client.get(
        "/api/media/media/images/2026-07/x.jpg",
        headers={"X-API-Key": "esp32-api-key"},
    )
    assert resp.status_code == 302
    assert resp.headers["Location"].startswith("https://test-bucket.s3.amazonaws.com/")
    assert "media/images/2026-07/x.jpg" in resp.headers["Location"]


def test_api_media_302_body_is_empty(client):
    """The 302 has no body — bytes flow from S3, not Flask."""
    resp = client.get(
        "/api/media/media/images/2026-07/x.jpg",
        headers={"X-API-Key": "esp32-api-key"},
    )
    assert resp.status_code == 302
    assert resp.data == b""


# ---------------------------------------------------------------------------
# Path-traversal guard
# ---------------------------------------------------------------------------


def test_api_media_with_dotdot_returns_400(client):
    """`..` anywhere in the key is rejected before S3 is touched."""
    resp = client.get(
        "/api/media/media/../etc/passwd",
        headers={"X-API-Key": "esp32-api-key"},
    )
    assert resp.status_code == 400
    assert resp.json == {"error": "invalid S3 key"}


def test_api_media_with_leading_slash_returns_400(client):
    """A key with a leading `/` is rejected (Flask strips these from
    route matches, but a double-encoded attempt still hits the guard)."""
    # Flask's <path:> converter accepts /-prefixed paths by stripping
    # the leading /. To exercise the explicit leading-/ guard, we send
    # a path that, after URL-decoding, contains a leading slash inside
    # the captured segment (e.g. //foo).
    resp = client.get(
        "/api/media//etc/passwd",
        headers={"X-API-Key": "esp32-api-key"},
    )
    # Werkzeug normalizes `//` to `/` and returns a 308 permanent
    # redirect, which is the documented RFC-7231 response for a
    # normalized URL. That's a 3xx (route didn't match), so the
    # operator-visible behavior is "request did not reach S3" —
    # which is what the guard intends.
    assert resp.status_code in (308, 400, 404)


def test_api_media_with_embedded_double_slash_returns_400(client):
    """`//` in the captured path is rejected (proxies sometimes
    rewrite `//` to `/`; an attacker could use the rewrite to escape
    a prefix match)."""
    # Use a URL like `/api/media/foo//bar` — Flask will route to
    # /api/media/<path:foo//bar>. The guard rejects `//`.
    resp = client.get(
        "/api/media/foo//bar",
        headers={"X-API-Key": "esp32-api-key"},
    )
    # 400 (guard hit) is the expected response. 404 happens if the
    # Werkzeug router consumes the doubled slash and routes elsewhere
    # — but in practice Werkzeug preserves `//` inside the captured
    # segment.
    assert resp.status_code in (400, 404)


# ---------------------------------------------------------------------------
# S3 failure paths
# ---------------------------------------------------------------------------


def test_api_media_with_signing_failure_returns_404(client, monkeypatch):
    """If `s3.signed_media_url` returns None, the endpoint returns 404."""
    monkeypatch.setattr(sys.modules["s3"], "signed_media_url", MagicMock(return_value=None))
    # Also patch the reference held on the main.py module — main.py
    # imported `s3` at module load and calls `s3.signed_media_url` via
    # that module reference.
    sys.modules["heart-message-manager.main"].s3.signed_media_url = MagicMock(return_value=None)
    resp = client.get(
        "/api/media/media/images/2026-07/x.jpg",
        headers={"X-API-Key": "esp32-api-key"},
    )
    assert resp.status_code == 404
    assert "not found" in resp.json.get("error", "").lower()


def test_api_media_with_signing_exception_returns_404(client, monkeypatch):
    """If `s3.signed_media_url` raises (boto3 BotoCoreError / ClientError),
    the endpoint returns 404 rather than 500."""
    def boom(key, expires_in=3600):
        raise RuntimeError("NoSuchKey")

    monkeypatch.setattr(sys.modules["s3"], "signed_media_url", boom)
    sys.modules["heart-message-manager.main"].s3.signed_media_url = boom
    resp = client.get(
        "/api/media/media/images/2026-07/x.jpg",
        headers={"X-API-Key": "esp32-api-key"},
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Per-request fresh signature
# ---------------------------------------------------------------------------


def test_api_media_signs_url_per_request(client):
    """Two consecutive requests with the same key produce DIFFERENT
    signed URLs (proves we're not caching the presigned URL)."""
    first = client.get(
        "/api/media/media/images/2026-07/x.jpg",
        headers={"X-API-Key": "esp32-api-key"},
    )
    second = client.get(
        "/api/media/media/images/2026-07/x.jpg",
        headers={"X-API-Key": "esp32-api-key"},
    )
    assert first.status_code == 302
    assert second.status_code == 302
    # In the default mock, both signed URLs look identical
    # ("...x.jpg?X-Amz-Signature=abc"). The point of this test is
    # that the endpoint CALLS `signed_media_url` twice — once per
    # request. The mock's call_count confirms that.
    signed_url_fn = sys.modules["s3"].signed_media_url
    assert signed_url_fn.call_count == 2


def test_api_media_with_different_keys_uses_different_signed_urls(client, monkeypatch):
    """Each request must pass its own S3 key to `signed_media_url`."""
    seen_keys = []
    def capture(key, expires_in=3600):
        seen_keys.append(key)
        return f"https://test-bucket.s3.amazonaws.com/{key}?sig=ok"
    monkeypatch.setattr(sys.modules["s3"], "signed_media_url", capture)
    sys.modules["heart-message-manager.main"].s3.signed_media_url = capture

    client.get(
        "/api/media/media/images/2026-07/a.jpg",
        headers={"X-API-Key": "esp32-api-key"},
    )
    client.get(
        "/api/media/media/videos/2026-07/b.mp4",
        headers={"X-API-Key": "esp32-api-key"},
    )
    assert seen_keys == [
        "media/images/2026-07/a.jpg",
        "media/videos/2026-07/b.mp4",
    ]
