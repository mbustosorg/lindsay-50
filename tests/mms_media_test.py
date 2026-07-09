"""Tests for the async MMS webhook path (issue #38 / openspec `mms-media-support`).

The webhook handler (`heart-message-manager/main.py:_process_inbound_message`)
must respond to Twilio with 200/TwiML *immediately* on MMS payloads (D13),
spawn a background thread to download attachments and upload them to OUR S3,
and only THEN publish the `MessageEnvelope` over MQTT. A Twilio retry with
the same `MessageSid` while the worker is running must NOT spawn a
duplicate background thread (spec scenario: MessageSid dedupe).
"""

from __future__ import annotations

import sys
import threading
import time
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


def _make_mock_cfg_no_twilio():
    """Same as `_make_mock_cfg` but with no TWILIO_AUTH_TOKEN — used for
    the `no-validator` variant of the test app."""
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
            "TWILIO_ACCOUNT_SID": "ACtest",
        }.get(k)
    )
    return cfg


def _load_app_module(mock_cfg, mqtt_publisher):
    """Load main.py with heavy I/O mocked but `lib_shared.models` real.

    Returns (mod, flask_app) where `mod` exposes module-level state we
    need to reach (executor, dedupe map, mocked publisher).
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

    # lib_shared.models: use the REAL module so MessageEnvelope / Message
    # construction is exercised end-to-end.
    from lib_shared import models as real_models  # noqa: WPS433 - intentional reimport
    sys.modules["lib_shared.models"] = real_models

    cm_mod = _make_mock("lib_shared.config_migrations")
    cm_mod.migrate = MagicMock(side_effect=lambda d, current_version: d or {})
    cm_mod.migrate_on_startup = MagicMock()

    mm_mod = _make_mock("lib_shared.message_manager")
    mm_mod.MessageManager = MagicMock()

    paho_mod = _make_mock("lib_shared.paho_mqtt_client")
    paho_mod.PahoMqttClient = MagicMock(return_value=mqtt_publisher)

    # Heart-message-manager modules
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
        side_effect=lambda ctype, url: (
            f"media/images/2026-07/test-{ctype.replace('/', '-')}.jpg"
            if ctype.startswith("image/")
            else f"media/videos/2026-07/test-{ctype.replace('/', '-')}.mp4"
        )
    )
    s3_mod.signed_media_url = MagicMock(
        side_effect=lambda key, expires_in=3600: f"https://test-bucket.s3.amazonaws.com/{key}?X-Amz-fake-signature=1"
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

    # Boot-time publishes (`_publish_check_for_update_once` +
    # `migrate_on_startup`'s config publish) happen during module load.
    # Reset the mock so the test only sees publishes caused by its
    # own webhook.
    mqtt_publisher.publish_envelope.reset_mock()

    mod._test_real_modules = real_modules
    return mod, flask_app


def _restore_modules(real_modules):
    for name, real in real_modules.items():
        sys.modules[name] = real
    for name in list(sys.modules):
        if (name == "lib_shared" or name.startswith("lib_shared.")) and name not in real_modules:
            sys.modules.pop(name, None)


def _wait_for_dedupe_release(mms_app, sid: str, timeout: float = 5.0) -> bool:
    mod, _ = mms_app
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with mod._INBOUND_DEDUPE_LOCK:
            if sid not in mod._INBOUND_DEDUPE:
                return True
        time.sleep(0.01)
    return False


def _build_app_for_test(mock_cfg):
    mqtt_publisher = MagicMock()
    mqtt_publisher.publish_envelope = MagicMock(return_value=True)
    mqtt_publisher.subscribe = MagicMock()
    mqtt_publisher.loop_start = MagicMock()
    mqtt_publisher.connect_async = MagicMock()
    mod, flask_app = _load_app_module(mock_cfg, mqtt_publisher)
    mod._INBOUND_DEDUPE.clear()
    mod._mqtt_client = mqtt_publisher

    def restore():
        if mod._MMS_EXECUTOR is not None:
            mod._MMS_EXECUTOR.shutdown(wait=False, cancel_futures=True)
        _restore_modules(mod._test_real_modules)

    return mod, flask_app, mqtt_publisher, restore


@pytest.fixture
def mms_app():
    """Yield (mod, app, mqtt_publisher) with TWILIO_AUTH_TOKEN set."""
    mock_cfg = _make_mock_cfg()
    mod, flask_app, mqtt_publisher, restore = _build_app_for_test(mock_cfg)
    try:
        yield mod, flask_app, mqtt_publisher
    finally:
        restore()


@pytest.fixture
def mms_app_no_validator():
    """Same as `mms_app` but TWILIO_AUTH_TOKEN is absent so the
    Twilio signature validator is bypassed."""
    mock_cfg = _make_mock_cfg_no_twilio()
    mod, flask_app, mqtt_publisher, restore = _build_app_for_test(mock_cfg)
    try:
        yield mod, flask_app, mqtt_publisher
    finally:
        restore()


# ---------------------------------------------------------------------------
# Section 4.1 — webhook → S3 round-trip
# ---------------------------------------------------------------------------


def test_mms_webhook_returns_200_immediately_with_async_upload(mms_app_no_validator):
    """POST /api/messages with NumMedia=1 returns 200 immediately,
    BEFORE the S3 upload completes (D13: <200ms wall clock)."""
    mod, app, _ = mms_app_no_validator
    client = app.test_client()

    started = threading.Event()
    finish = threading.Event()

    def slow_log_media(ctype, url):
        started.set()
        finish.wait(timeout=2.0)
        return "media/images/2026-07/x.jpg"

    sys.modules["s3"].log_media = slow_log_media
    sys.modules["heart-message-manager.main"].s3.log_media = slow_log_media

    sid = "MM-deadbeef-1"
    start = time.monotonic()
    resp = client.post(
        "/api/messages",
        data={
            "From": "+15551234567",
            "Body": "With a pic!",
            "MessageSid": sid,
            "NumMedia": "1",
            "MediaContentType0": "image/jpeg",
            "MediaUrl0": "https://api.twilio.com/.../ME19.jpg",
        },
    )
    elapsed_ms = (time.monotonic() - start) * 1000.0
    assert resp.status_code == 200
    assert elapsed_ms < 200.0, f"webhook took {elapsed_ms:.1f}ms"
    assert started.wait(timeout=2.0), "background thread never reached log_media"

    finish.set()
    assert _wait_for_dedupe_release((mod, app), sid)


def test_mms_webhook_publishes_envelope_with_media_after_uploads(mms_app_no_validator):
    """Background thread persists the Message (text + media list) then
    publishes over MQTT — exactly once."""
    mod, app, mqtt_publisher = mms_app_no_validator
    client = app.test_client()

    sid = "MM-deadbeef-2"
    resp = client.post(
        "/api/messages",
        data={
            "From": "+15551234567",
            "Body": "Look at this",
            "MessageSid": sid,
            "NumMedia": "1",
            "MediaContentType0": "image/jpeg",
            "MediaUrl0": "https://api.twilio.com/.../ME19.jpg",
        },
    )
    assert resp.status_code == 200
    assert _wait_for_dedupe_release((mod, app), sid)

    assert mqtt_publisher.publish_envelope.call_count == 1
    envelope = mqtt_publisher.publish_envelope.call_args.args[0]
    assert envelope.type == "message"
    payload = envelope.payload
    assert payload["sender"] == "+15551234567"
    assert payload["body"] == "Look at this"
    assert len(payload["media"]) == 1
    assert payload["media"][0]["type"] == "image/jpeg"
    assert payload["media"][0]["url"].startswith("media/images/")


# ---------------------------------------------------------------------------
# Section 4.1b — parallel uploads + mixed content types
# ---------------------------------------------------------------------------


def test_mms_webhook_with_mixed_content_types_uses_parallel_uploads(mms_app_no_validator):
    """Two MMS attachments (image/png + video/mp4) → both land in the
    media list with their distinct S3 key prefixes."""
    mod, app, mqtt_publisher = mms_app_no_validator
    client = app.test_client()
    sid = "MM-mixed-1"
    resp = client.post(
        "/api/messages",
        data={
            "From": "+15551234567",
            "Body": "Both",
            "MessageSid": sid,
            "NumMedia": "2",
            "MediaContentType0": "image/png",
            "MediaUrl0": "https://api.twilio.com/.../ME_png",
            "MediaContentType1": "video/mp4",
            "MediaUrl1": "https://api.twilio.com/.../ME_mp4",
        },
    )
    assert resp.status_code == 200
    assert _wait_for_dedupe_release((mod, app), sid)

    assert mqtt_publisher.publish_envelope.call_count == 1
    payload = mqtt_publisher.publish_envelope.call_args.args[0].payload
    assert len(payload["media"]) == 2
    by_url = {m["url"]: m["type"] for m in payload["media"]}
    assert any(v.startswith("media/images/") for v in by_url)
    assert any(v.startswith("media/videos/") for v in by_url)
    types = {m["type"] for m in payload["media"]}
    assert types == {"image/png", "video/mp4"}


# ---------------------------------------------------------------------------
# Section 4.2 — S3-failure path
# ---------------------------------------------------------------------------


def test_mms_webhook_with_partial_s3_failure_drops_failed_items(mms_app_no_validator):
    """`log_media` returns None for one of N attachments → webhook still
    200s; after dedupe release, Message has N-1 items."""
    mod, app, mqtt_publisher = mms_app_no_validator

    def flaky_log_media(ctype, url):
        if "ME_fail" in url:
            return None
        return f"media/{('images' if ctype.startswith('image/') else 'videos')}/2026-07/x.{ctype.split('/')[-1]}"

    sys.modules["s3"].log_media = flaky_log_media
    sys.modules["heart-message-manager.main"].s3.log_media = flaky_log_media

    client = app.test_client()
    sid = "MM-partial-1"
    resp = client.post(
        "/api/messages",
        data={
            "From": "+15551234567",
            "Body": "Some fail",
            "MessageSid": sid,
            "NumMedia": "2",
            "MediaContentType0": "image/jpeg",
            "MediaUrl0": "https://api.twilio.com/.../ME_ok.jpg",
            "MediaContentType1": "image/png",
            "MediaUrl1": "https://api.twilio.com/.../ME_fail.png",
        },
    )
    assert resp.status_code == 200
    assert _wait_for_dedupe_release((mod, app), sid)

    assert mqtt_publisher.publish_envelope.call_count == 1
    payload = mqtt_publisher.publish_envelope.call_args.args[0].payload
    assert len(payload["media"]) == 1
    assert payload["media"][0]["type"] == "image/jpeg"


def test_mms_webhook_with_total_s3_failure_publishes_text_only(mms_app_no_validator):
    """Every `log_media` returns None → message publishes with empty media."""
    mod, app, mqtt_publisher = mms_app_no_validator

    def always_fail(ctype, url):
        return None

    sys.modules["s3"].log_media = always_fail
    sys.modules["heart-message-manager.main"].s3.log_media = always_fail

    client = app.test_client()
    sid = "MM-all-fail"
    resp = client.post(
        "/api/messages",
        data={
            "From": "+15551234567",
            "Body": "All attachments dropped",
            "MessageSid": sid,
            "NumMedia": "2",
            "MediaContentType0": "image/jpeg",
            "MediaUrl0": "https://api.twilio.com/.../ME1.jpg",
            "MediaContentType1": "image/png",
            "MediaUrl1": "https://api.twilio.com/.../ME2.png",
        },
    )
    assert resp.status_code == 200
    assert _wait_for_dedupe_release((mod, app), sid)

    assert mqtt_publisher.publish_envelope.call_count == 1
    payload = mqtt_publisher.publish_envelope.call_args.args[0].payload
    assert payload["body"] == "All attachments dropped"
    assert payload["media"] == []


# ---------------------------------------------------------------------------
# Section 4.1c — media-only (no body) is accepted
# ---------------------------------------------------------------------------


def test_mms_webhook_media_only_publishes_text_empty_but_with_media(mms_app_no_validator):
    """Body="" + NumMedia=1 still publishes a Message with the media
    list populated — D10 says don't drop the message just because the
    caption is empty."""
    mod, app, mqtt_publisher = mms_app_no_validator
    client = app.test_client()
    sid = "MM-media-only"
    resp = client.post(
        "/api/messages",
        data={
            "From": "+15551234567",
            "Body": "",
            "MessageSid": sid,
            "NumMedia": "1",
            "MediaContentType0": "image/jpeg",
            "MediaUrl0": "https://api.twilio.com/.../ME_only.jpg",
        },
    )
    assert resp.status_code == 200
    assert _wait_for_dedupe_release((mod, app), sid)

    assert mqtt_publisher.publish_envelope.call_count == 1
    payload = mqtt_publisher.publish_envelope.call_args.args[0].payload
    assert payload["body"] == ""
    assert len(payload["media"]) == 1


def test_sms_only_webhook_returns_204(mms_app_no_validator):
    """Body="" with NumMedia=0 → 204, no publish, no background."""
    mod, app, mqtt_publisher = mms_app_no_validator
    client = app.test_client()
    resp = client.post(
        "/api/messages",
        data={"From": "+15551234567", "Body": "", "NumMedia": "0"},
    )
    assert resp.status_code == 204
    assert mqtt_publisher.publish_envelope.call_count == 0


# ---------------------------------------------------------------------------
# Section 4.4 — Twilio retry dedupe
# ---------------------------------------------------------------------------


def test_mms_webhook_with_duplicate_sid_does_not_spawn_duplicate_worker(mms_app_no_validator):
    """Two POSTs with the same MessageSid in quick succession — the
    second sees the SID in the dedupe guard, returns 200 immediately
    without spawning a duplicate background thread. Only one
    MessageEnvelope publishes."""
    mod, app, mqtt_publisher = mms_app_no_validator
    slow_event = threading.Event()
    slow_started = threading.Event()
    call_count = 0
    lock = threading.Lock()

    def slow_log_media(ctype, url):
        nonlocal call_count
        with lock:
            call_count += 1
        slow_started.set()
        slow_event.wait(timeout=3.0)
        return "media/images/2026-07/x.jpg"

    sys.modules["s3"].log_media = slow_log_media
    sys.modules["heart-message-manager.main"].s3.log_media = slow_log_media

    client = app.test_client()
    sid = "MM-retry-1"
    params = {
        "From": "+15551234567",
        "Body": "First",
        "MessageSid": sid,
        "NumMedia": "1",
        "MediaContentType0": "image/jpeg",
        "MediaUrl0": "https://api.twilio.com/.../ME_a.jpg",
    }
    first = client.post("/api/messages", data=params)
    assert first.status_code == 200
    assert slow_started.wait(timeout=2.0), "first worker never reached log_media"

    # Retry the SAME SID while the first worker is still blocked in log_media.
    second = client.post("/api/messages", data={**params, "Body": "Retry"})
    assert second.status_code == 200
    assert call_count == 1

    slow_event.set()
    assert _wait_for_dedupe_release((mod, app), sid)
    assert mqtt_publisher.publish_envelope.call_count == 1


# ---------------------------------------------------------------------------
# Section 4.4 — crash-recovery
# ---------------------------------------------------------------------------


def test_mms_webhook_does_not_deadlock_when_worker_crashes(mms_app_no_validator):
    """A background-thread exception must release the dedupe slot —
    a follow-up webhook with the same SID should process normally."""
    mod, app, mqtt_publisher = mms_app_no_validator

    def boom(ctype, url):
        raise RuntimeError("simulated S3 outage")

    sys.modules["s3"].log_media = boom
    sys.modules["heart-message-manager.main"].s3.log_media = boom

    client = app.test_client()
    sid = "MM-crash-1"
    resp = client.post(
        "/api/messages",
        data={
            "From": "+15551234567",
            "Body": "Crash",
            "MessageSid": sid,
            "NumMedia": "1",
            "MediaContentType0": "image/jpeg",
            "MediaUrl0": "https://api.twilio.com/.../ME.jpg",
        },
    )
    assert resp.status_code == 200
    assert _wait_for_dedupe_release((mod, app), sid, timeout=3.0)

    resp2 = client.post(
        "/api/messages",
        data={
            "From": "+15551234567",
            "Body": "Crash retry",
            "MessageSid": sid,
            "NumMedia": "1",
            "MediaContentType0": "image/jpeg",
            "MediaUrl0": "https://api.twilio.com/.../ME.jpg",
        },
    )
    assert resp2.status_code == 200
    assert _wait_for_dedupe_release((mod, app), sid, timeout=3.0)


# ---------------------------------------------------------------------------
# Section 4.4 — webhook response latency
# ---------------------------------------------------------------------------


def test_mms_webhook_response_returns_within_200ms(mms_app_no_validator):
    """With 3 attachments in the background, the request itself
    returns in <200ms (D13 budget)."""
    mod, app, mqtt_publisher = mms_app_no_validator
    client = app.test_client()
    slow_event = threading.Event()
    slow_started = threading.Event()

    def slow_log_media(ctype, url):
        slow_started.set()
        slow_event.wait(timeout=2.0)
        return f"media/{('images' if ctype.startswith('image/') else 'videos')}/2026-07/x.{ctype.split('/')[-1]}"

    sys.modules["s3"].log_media = slow_log_media
    sys.modules["heart-message-manager.main"].s3.log_media = slow_log_media

    sid = "MM-latency"
    payload = {
        "From": "+15551234567",
        "Body": "Slow",
        "MessageSid": sid,
        "NumMedia": "3",
    }
    for i in range(3):
        payload[f"MediaContentType{i}"] = "image/jpeg"
        payload[f"MediaUrl{i}"] = f"https://api.twilio.com/.../ME_{i}.jpg"

    start = time.monotonic()
    resp = client.post("/api/messages", data=payload)
    elapsed_ms = (time.monotonic() - start) * 1000.0
    assert resp.status_code == 200
    assert elapsed_ms < 200.0, f"webhook took {elapsed_ms:.1f}ms (>200ms budget)"

    slow_started.wait(timeout=1.0)
    slow_event.set()
    assert _wait_for_dedupe_release((mod, app), sid)
