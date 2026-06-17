"""Boot a real Flask server for the /preview manual browser checks.

Uses the same mocked-deps pattern as tests/test_auth.py, but starts a
genuine WSGI server (Flask's built-in dev server) instead of a test
client. The browser can then hit http://localhost:5050/preview with
a real session cookie.

Heavy deps (MQTT, S3) are stubbed so we don't need real network access.
The /api/messages webhook is preserved so curl can inject messages; the
browser no longer polls /api/live-messages (that endpoint was removed
in v2 in favor of browser-side MQTT-WS). The /api/config endpoint is
preserved so the browser's seed fetch has something to call.
"""

import importlib.util
import os
import sys
import threading
import types
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

REPO_ROOT = Path(__file__).parent.parent
HMM_DIR = REPO_ROOT / "heart-message-manager"
# Put both the repo root (for lib_shared) and the heart-message-manager
# dir (for sqlite / s3 / auth) on sys.path so main.py's bare imports
# (import sqlite, import s3, import auth) resolve.
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(HMM_DIR))

# ---- mock lib_shared side-effects ----
mock_cfg = MagicMock()
mock_cfg.MQTT_CLIENT = "paho"
mock_cfg.MQTT_HOST = "localhost"
mock_cfg.MQTT_PORT = 1883
mock_cfg.MQTT_USERNAME = "test"
mock_cfg.MQTT_PASSWORD = "test"
mock_cfg.MQTT_TOPIC = "test"
mock_cfg.AWS_ACCESS_KEY_ID = "test"
mock_cfg.AWS_SECRET_ACCESS_KEY = "test"
mock_cfg.AWS_S3_BUCKET = "test"
mock_cfg.AWS_S3_REGION = "us-east-1"
mock_cfg.CONFIG_API_URL = "http://localhost/api/config"
mock_cfg.MESSAGES_API_URL = "http://localhost/api/messages"
mock_cfg.PORT = "5050"
mock_cfg.if_exists = MagicMock(
    side_effect=lambda k: {
        "ADMIN_USERNAME": "admin",
        "ADMIN_PASSWORD": "secret123",
        "API_SECRET_KEY": "esp32-api-key",
        "ADMIN_SESSION_TIMEOUT_MINS": "60",
        "TWILIO_AUTH_TOKEN": "",  # disable Twilio sig validation for curl injection
    }.get(k)
)


def _make_mock(name):
    mod = types.ModuleType(name)
    sys.modules[name] = mod
    return mod


# lib_shared
# Import the real models module directly (it has no internal lib_shared
# imports so it loads standalone). Inject it into sys.modules under the
# fully-qualified name so main.py's `from lib_shared.models import ...`
# resolves to the real Message class.
real_lib_shared_models = importlib.import_module("lib_shared.models")
sys.modules["lib_shared.models"] = real_lib_shared_models

make_mock = _make_mock
make_mock("lib_shared")  # no-op parent, real submodules win
config_reader_mod = make_mock("lib_shared.config_reader")
config_reader_mod.get_config = lambda required_keys=None: mock_cfg
log_setup_mod = make_mock("lib_shared.log_setup")
log_setup_mod.configure_logging = MagicMock()
# main.py no longer imports lib_shared.message_manager (v2 removed the
# Flask-side MessageManager), but we still stub the module in case any
# helper references it.
mm_mod = make_mock("lib_shared.message_manager")
mm_mod.MessageManager = MagicMock()
paho_mod = make_mock("lib_shared.paho_mqtt_client")
paho_mod.PahoMqttClient = MagicMock()

# heart-message-manager submodules
paho_mod = types.ModuleType("paho_mqtt_client")
paho_mod.PahoMqttClient = MagicMock()
sys.modules["paho_mqtt_client"] = paho_mod

# Load main.py
_main_path = REPO_ROOT / "heart-message-manager" / "main.py"
spec = importlib.util.spec_from_file_location("preview_server_main", str(_main_path))
mod = importlib.util.module_from_spec(spec)
sys.modules["heart-message-manager.main"] = mod
spec.loader.exec_module(mod)

# Make sqlite.get_messages_since and get_all_messages return a list of
# fake messages so /api/messages has something to return when the
# browser's MessageManager seeds itself.
import sqlite as sqlite_real

RealMessage = real_lib_shared_models.Message

# Monkey-patch sqlite module's get_all_messages to return our fake list
_fake_messages = []
_fake_lock = threading.Lock()


def _make_msg(body, msg_id=None):
    from uuid import uuid4

    return RealMessage(
        id=msg_id or str(uuid4()),
        sender="+15551234567",
        body=body,
        received_at=datetime.now(timezone.utc).isoformat(),
    )


def _add_fake_message(body):
    with _fake_lock:
        _fake_messages.insert(0, _make_msg(body))


def _get_all_messages():
    with _fake_lock:
        return list(_fake_messages)  # return the Message objects directly


# Patch the sqlite functions on the mod's namespace (it's bound to `import sqlite`)
sqlite_real.get_all_messages = _get_all_messages
sqlite_real.get_message = lambda mid: next((m for m in _fake_messages if m.id == mid), None)
sqlite_real.get_config = MagicMock(
    return_value=type(
        "Cfg",
        (),
        {
            "sign": type("Sign", (), {"name": "Lindsay's Heart"})(),
            "timezone": "America/Los_Angeles",
            "filters": [],
            "rendering": type("R", (), {"mode": "scroll", "speed": 0.04, "color": 0xFF0000})(),
            "senders": {},
        },
    )()
)
sqlite_real.message_count = lambda: len(_fake_messages)
sqlite_real.put_message = lambda msg: _add_fake_message(msg.body)
sqlite_real.put_config = MagicMock()
sqlite_real.get_messages_since = lambda since: _get_all_messages()

# Same for s3
_make_mock("s3")
import s3 as s3_real

s3_real.log_message = MagicMock()
s3_real.save_config_snapshot = MagicMock()
s3_real._s3_bucket = MagicMock(return_value="test-bucket")
s3_real._s3_client = MagicMock()
s3_real.load_messages_from_s3 = MagicMock(return_value=[])
s3_real.load_latest_config = MagicMock(return_value=None)

# Pre-seed two messages so the preview has something to show
_add_fake_message("hello from preview-server.py")
_add_fake_message("second seeded message")

# Point Flask's Jinja loader at the real templates directory
from jinja2 import FileSystemLoader

mod.app.jinja_env = mod.app.create_jinja_environment()
mod.app.jinja_env.loader = FileSystemLoader(str(REPO_ROOT / "heart-message-manager" / "templates"))

# Flask derives root_path from the module name, which in this
# importlib-loaded context is the repo root, not heart-message-manager.
# Override static_folder so /static/preview/* resolves correctly.
mod.app.static_folder = str(HMM_DIR / "static")
mod.app.static_url_path = "/static"

# Start the server
print(f"Starting preview server on http://localhost:{mock_cfg.PORT}")
print(f"Login:  POST /login  username=admin  password=secret123")
print(f"Page:   GET  /preview")
print(f"Inject: POST /api/messages  From=...&Body=...")
print(f"Seed:   GET  /api/messages  (X-API-Key auth)")
print(f"PID:    {os.getpid()}")

# Run Flask's dev server (no debug reloader, no signal handler)
mod.app.run(host="127.0.0.1", port=int(mock_cfg.PORT), debug=False, use_reloader=False)
