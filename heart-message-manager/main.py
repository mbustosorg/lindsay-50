"""Flask app for heart-message-manager.

Handles Twilio webhooks, stores messages to SQLite (with S3 backup),
publishes to Adafruit IO, and serves the admin UI.
"""

from __future__ import annotations

import functools
import html
import logging
import uuid
from pathlib import Path
import sys
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

sys.path.insert(0, str(Path(__file__).parent.parent))

from flask import (
    Flask,
    Response,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import login_required

# Load config before any lib imports that call get_config() at module level
from lib_shared.config_reader import get_config

REQUIRED_KEYS: set[str] = {
    "MQTT_HOST",
    "MQTT_PORT",
    "MQTT_USERNAME",
    "MQTT_PASSWORD",
    "MQTT_TOPIC",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_S3_BUCKET",
    "AWS_S3_REGION",
    "CONFIG_API_URL",
    "MESSAGES_API_URL",
}
_cfg = get_config(REQUIRED_KEYS)

import sqlite, s3
from server_time import format_from_iso, now_utc_iso
from lib_shared.models import SignConfig, FilterRule, Message
from lib_shared.models import MessageEnvelope

# App setup
app = Flask(__name__)
_secret_key_path = Path(__file__).parent / "secret_key"
if _secret_key_path.exists():
    app.secret_key = _secret_key_path.read_text()
else:
    app.secret_key = uuid.uuid4().hex
    _secret_key_path.write_text(str(app.secret_key))

# Init auth (Flask-Login + API key + sliding session)
import auth

auth.init_app(app)


def api_login_required(f):
    """Decorator: require either API key auth (X-API-Key header) or session auth."""

    @functools.wraps(f)
    def decorated_function(*args, **kwargs):
        if getattr(g, "api_key_auth", False):
            return f(*args, **kwargs)
        from flask_login import current_user

        if not current_user.is_authenticated:
            from flask import jsonify

            return jsonify({"error": "missing API key"}), 401
        return f(*args, **kwargs)

    return decorated_function


from lib_shared.log_setup import configure_logging

configure_logging(logging.INFO)
logger = logging.getLogger(__name__)


# Wipe SQLite and rebuild messages and config from S3 on startup
sqlite.rebuild_from_s3(s3.load_messages_from_s3, s3.load_latest_config)


# Platform MQTT client (paho on every platform) — used only as a publisher
# (no Flask-side subscriber). The device and the browser both subscribe to
# the broker on their own.
from lib_shared.paho_mqtt_client import PahoMqttClient


def _noop_dispatch(_payload: str) -> None:
    """Flask no longer subscribes to MQTT envelopes; drop them on the floor."""


_mqtt_client = PahoMqttClient(_noop_dispatch)
logger.info("Starting MQTT client at boot...")
_mqtt_client.start()


# Inbound Twilio webhook handler
@app.route("/api/messages", methods=["POST"])
def api_messages():
    """Receive Twilio webhook: verify signature → log to S3 → respond → store → publish."""
    twilio_token = _cfg.if_exists("TWILIO_AUTH_TOKEN")
    if twilio_token:
        from twilio.request_validator import RequestValidator

        # Reconstruct URL with the scheme Twilio actually used (from X-Forwarded-Proto)
        # Heroku terminates TLS and forwards over HTTP internally, so we can't use
        # request.scheme directly — we must trust X-Forwarded-Proto.
        forwarded_proto = request.headers.get("X-Forwarded-Proto", "http")
        webhook_url = f"{forwarded_proto}://{request.host}/api/messages"

        validator = RequestValidator(twilio_token)
        signature = request.headers.get("X-Twilio-Signature", "")
        params = request.form.to_dict()
        logger.info(
            "Twilio validation: reconstructed_url=%s X-Forwarded-Proto=%s",
            webhook_url,
            forwarded_proto,
        )
        if not validator.validate(webhook_url, params, signature):
            logger.warning("Twilio signature verification failed for %s", webhook_url)
            return Response("forbidden", status=403)

    return _process_inbound_message(request)


@app.route("/api/test-messages", methods=["POST"])
@login_required
def api_test_messages():
    """Test injection from the admin UI — authenticated, skips Twilio signature validation."""
    return _process_inbound_message(request)


def _process_inbound_message(request) -> Response:
    """Shared message processing for both real Twilio webhooks and test injections."""
    sender = request.form.get("From", "")
    body = request.form.get("Body", "").strip()

    logger.info("From=%r Body=%r", sender, body)

    if not body:
        return Response("", status=204)

    msg = Message(
        id=str(uuid.uuid4()),
        sender=sender,
        body=body,
        received_at=now_utc_iso(),
    )

    try:
        s3.log_message(msg)
    except Exception as e:
        logger.warning("S3 logging failed (will continue): %s", e)

    cfg = sqlite.get_config()
    sign_name = cfg.sign.name if cfg.sign else "Lindsay's Heart"
    reply = f"{sign_name} got your message: {html.escape(body)}"
    twiml = Response(
        f"<Response><Message>{reply}</Message></Response>",
        status=200,
        mimetype="text/xml",
    )

    try:
        sqlite.put_message(msg)
        assert _mqtt_client is not None
        _mqtt_client.publish_envelope(MessageEnvelope("message", msg.to_dict()))
    except Exception as e:
        logger.error("Post-webhook processing failed: %s", e)

    return twiml


# API Endpoints
@app.route("/api/messages", methods=["GET"])
@api_login_required
def api_get_messages():
    """GET /api/messages?since=<timestamp> — return messages as JSON."""
    since = request.args.get("since")
    if since:
        msgs = sqlite.get_messages_since(since)
    else:
        msgs = sqlite.get_all_messages()
    return jsonify([m.to_dict() for m in msgs])


@app.route("/api/messages/<msg_id>/suppress", methods=["POST"])
@api_login_required
def api_suppress(msg_id):
    """Add a type=message filter rule to suppress the given message (JSON API)."""
    msg = sqlite.get_message(msg_id)
    if msg is None:
        return jsonify({"error": "message not found"}), 404

    added = _suppress_message(msg_id)
    if not added:
        return jsonify({"status": "already suppressed"})
    return jsonify(
        {
            "status": "ok",
            "filter_added": {
                "type": "message",
                "pattern": msg_id,
                "action": "suppress",
            },
        }
    )


@app.route("/api/messages/<msg_id>/unsuppress", methods=["POST"])
@api_login_required
def api_unsuppress(msg_id):
    """Remove type=message filter rule for the given message (JSON API)."""
    removed = _unsuppress_message(msg_id)
    if not removed:
        return jsonify({"status": "not found"}), 404
    return jsonify({"status": "ok"})


@app.route("/messages/<msg_id>/suppress", methods=["POST"])
@login_required
def web_suppress(msg_id):
    """Web-form wrapper for suppress that redirects back to the messages page."""
    _suppress_message(msg_id)
    return redirect(url_for("message_list"))


@app.route("/messages/<msg_id>/unsuppress", methods=["POST"])
@login_required
def web_unsuppress(msg_id):
    """Web-form wrapper for unsuppress that redirects back to the messages page."""
    _unsuppress_message(msg_id)
    return redirect(url_for("message_list"))


@app.route("/api/config", methods=["GET"])
@api_login_required
def api_get_config():
    """Return current config as JSON."""
    cfg = sqlite.get_config()
    return jsonify(cfg.to_dict())


@app.route("/api/config", methods=["PUT"])
@api_login_required
def api_put_config():
    """Accept full config JSON, store to SQLite, snapshot S3, publish to Adafruit IO."""
    try:
        data = request.get_json()
    except Exception:
        return jsonify({"error": "invalid JSON"}), 400

    if not isinstance(data, dict):
        return jsonify({"error": "expected JSON object"}), 400

    cfg = SignConfig.from_dict(data)
    _save_and_publish(cfg)
    return jsonify({"status": "ok"})


# Admin API (S3 browser)


@app.route("/api/admin/s3-objects")
@api_login_required
def api_s3_objects():
    """Return S3 objects under a prefix as a tree node list."""
    try:
        bucket = s3._s3_bucket()
        client = s3._s3_client()
        prefix = request.args.get("prefix", "")
        response = client.list_objects_v2(Bucket=bucket, Prefix=prefix, Delimiter="/")

        nodes = []

        for cp in response.get("CommonPrefixes", []):
            folder = cp["Prefix"].rstrip("/")
            name = folder.split("/")[-1]
            folder_id = cp["Prefix"].rstrip("/") + "/"
            nodes.append(
                {
                    "id": folder_id,
                    "text": name,
                    "children": True,
                }
            )

        for obj in response.get("Contents", []):
            key = obj["Key"]
            name = key.split("/")[-1]
            size = obj["Size"]
            size_str = str(size) if size < 1024 else f"{size / 1024:.1f}KB"
            nodes.append(
                {
                    "id": key,
                    "text": f"{name} ({size_str})",
                    "children": False,
                }
            )

        # Sort descending by key (chronological, newest first)
        nodes.sort(key=lambda n: n["id"], reverse=True)

        return jsonify({"bucket": bucket, "prefix": prefix, "nodes": nodes})
    except Exception as e:
        logger.warning("S3 list failed: %s", e)
        err_str = str(e)
        if "NoSuchBucket" in err_str:
            return (
                jsonify({"error": "Bucket does not exist. Create it in MinIO console."}),
                500,
            )
        return jsonify({"error": err_str}), 500


@app.route("/api/admin/s3-object")
@api_login_required
def api_s3_object():
    """Fetch and return the content of a specific S3 object."""
    key = request.args.get("key", "")
    if not key:
        return jsonify({"error": "key parameter required"}), 400
    try:
        bucket = s3._s3_bucket()
        client = s3._s3_client()
        response = client.get_object(Bucket=bucket, Key=key)
        content = response["Body"].read().decode()
        return jsonify({"key": key, "content": content})
    except Exception as e:
        logger.warning("S3 get failed: %s", e)
        return jsonify({"error": str(e)}), 500


# Helpers


def _save_and_publish(cfg: SignConfig) -> None:
    """Save config to SQLite, snapshot S3, publish to Adafruit IO."""
    sqlite.put_config(cfg)
    try:
        s3.save_config_snapshot(cfg.to_dict())
    except Exception as e:
        logger.warning("Config S3 snapshot failed: %s", e)
    assert _mqtt_client is not None
    _mqtt_client.publish_envelope(MessageEnvelope("config", cfg.to_dict()))


def _suppress_message(msg_id: str) -> bool:
    """Add type=message filter rule. Returns True if newly added."""
    msg = sqlite.get_message(msg_id)
    if msg is None:
        return False
    cfg = sqlite.get_config()
    for f in cfg.filters:
        if f.type == "message" and f.pattern == msg_id:
            return False
    cfg.filters.append(FilterRule(type="message", pattern=msg_id, action="suppress"))
    _save_and_publish(cfg)
    return True


def _unsuppress_message(msg_id: str) -> bool:
    """Remove type=message filter rule. Returns True if found and removed."""
    cfg = sqlite.get_config()
    original_len = len(cfg.filters)
    cfg.filters = [f for f in cfg.filters if not (f.type == "message" and f.pattern == msg_id)]
    if len(cfg.filters) == original_len:
        return False
    _save_and_publish(cfg)
    return True


# UI Routes
@app.route("/")
@login_required
def dashboard():
    """Dashboard: recent messages and counts."""
    msgs = sqlite.get_all_messages()[:20]
    cfg = sqlite.get_config()
    total = sqlite.message_count()

    suppression_counts = {}
    for f in cfg.filters:
        suppression_counts[f.type] = suppression_counts.get(f.type, 0) + 1

    return render_template(
        "dashboard.html",
        messages=msgs[:20],
        total_count=total,
        suppression_counts=suppression_counts,
        sign_name=cfg.sign.name if cfg.sign else "Lindsay's Heart",
        timezone=cfg.timezone,
        format_from_iso=format_from_iso,
    )


@app.route("/messages")
@login_required
def message_list():
    """Paginated message list with suppress/unsuppress buttons."""
    page = max(1, int(request.args.get("page", 1)))
    per_page = 50

    all_msgs = sqlite.get_all_messages()
    total = len(all_msgs)
    total_pages = max(1, (total + per_page - 1) // per_page)

    start = (page - 1) * per_page
    end = start + per_page
    page_msgs = all_msgs[start:end]

    cfg = sqlite.get_config()

    return render_template(
        "messages.html",
        messages=page_msgs,
        page=page,
        total_pages=total_pages,
        cfg=cfg,
        sign_name=cfg.sign.name if cfg.sign else "Lindsay's Heart",
        format_from_iso=format_from_iso,
    )


@app.route("/filters")
@login_required
def filter_rules():
    """Redirect old filter_rules URL to settings."""
    return redirect(url_for("settings"))


@app.route("/settings", methods=["GET", "POST"])
@login_required
def settings():
    """Allowed senders, rendering defaults, sign name, and filter rules."""
    cfg = sqlite.get_config()

    if request.method == "POST":
        # Filter rules (before general settings so saves happen once)
        filter_action = request.form.get("filter_action")
        if filter_action == "add":
            ftype = request.form.get("filter_type", "").strip()
            pattern = request.form.get("filter_pattern", "").strip()
            if ftype in ("keyword", "regex", "sender", "message") and pattern:
                cfg.filters.append(FilterRule(type=ftype, pattern=pattern, action="suppress"))
                _save_and_publish(cfg)
                return redirect(url_for("settings"))
        elif filter_action == "delete":
            idx = int(request.form.get("filter_index", -1))
            if 0 <= idx < len(cfg.filters):
                cfg.filters.pop(idx)
                _save_and_publish(cfg)
                return redirect(url_for("settings"))

        sign_name = request.form.get("sign_name", "").strip()
        if sign_name:
            cfg.sign.name = sign_name

        timezone = request.form.get("timezone", "").strip()
        if timezone:
            try:
                ZoneInfo(timezone)
                cfg.timezone = timezone
            except ZoneInfoNotFoundError:
                pass  # ignore invalid timezone, keep current value

        cfg.rendering.mode = request.form.get("rendering_mode", cfg.rendering.mode)
        try:
            cfg.rendering.speed = float(request.form.get("rendering_speed", cfg.rendering.speed))
        except ValueError:
            pass
        try:
            cfg.rendering.color = int(request.form.get("rendering_color", str(cfg.rendering.color)), 0)
        except ValueError:
            pass

        names = request.form.getlist("sender_name")
        phones = request.form.getlist("sender_phone")
        new_senders = {}
        for name, phone in zip(names, phones):
            name = name.strip()
            phone = phone.strip()
            if phone:
                new_senders[phone] = name or phone
        cfg.senders = new_senders

        _save_and_publish(cfg)
        return redirect(url_for("settings"))

    return render_template(
        "settings.html",
        cfg=cfg,
        sign_name=cfg.sign.name if cfg.sign else "Lindsay's Heart",
    )


@app.route("/preview")
@login_required
def preview():
    """Preview: show what display_list() returns, with toggle for include_filtered."""
    include_filtered = request.args.get("include_filtered", "false") == "true"
    cfg = sqlite.get_config()
    all_msgs = sqlite.get_all_messages()

    return render_template(
        "preview.html",
        result=all_msgs,
        include_filtered=include_filtered,
        sign_name=cfg.sign.name if cfg.sign else "Lindsay's Heart",
        timezone=cfg.timezone,
        format_from_iso=format_from_iso,
    )


@app.route("/testing")
@login_required
def testing():
    """Testing: inject messages and inspect system state."""
    cfg = sqlite.get_config()
    twilio_token = _cfg.if_exists("TWILIO_AUTH_TOKEN")
    return render_template(
        "testing.html",
        sign_name=cfg.sign.name,
        twilio_token=twilio_token or "",
    )


@app.route("/health")
def health():
    """Return a minimal 200 response for load-balancer health checks."""
    return "ok"


# CSP for the preview page: PyScript loads WebAssembly (needs
# wasm-unsafe-eval) and pulls its runtime + dependencies from
# pyscript.net and cdn.jsdelivr.net. The same-origin allowance covers
# the static files Flask ships under /static/ (the python source for the
# browser render path).
#
# `connect-src` must allow cdn.jsdelivr.net and pyscript.net because
# Pyodide fetches its WASM, the python stdlib zip, the package lockfile,
# and MicroPython worker JS from there. Without these, PyScript 2024.9.x
# silently fails to instantiate Pyodide — the page sits on "Loading
# preview…" forever and the browser console fills with
# "Connecting to 'https://cdn.jsdelivr.net/...' violates the
# Content Security Policy directive: 'connect-src 'self''" errors.
#
# It must ALSO allow the MQTT-over-WebSocket origin. The browser-side
# `mqtt_ws_client.js` opens a WS to `MQTT_WS_URL` (derived from
# `MQTT_HOST` — `ws://localhost:9002` for local dev,
# `wss://io.adafruit.com` for Adafruit IO). Without this, the browser
# console fills with "Connecting to 'ws://localhost:9002/mqtt' violates
# the following Content Security Policy directive" errors and the
# preview page never receives live envelopes.
_PREVIEW_CSP_BASE = (
    "default-src 'self'; "
    # 'unsafe-inline' + cdn.tailwindcss.com: base.html loads the Tailwind
    # play CDN and an inline `tailwind.config = {...}` block. The /preview
    # route is login-protected, so allowing these is safe.
    "script-src 'self' 'unsafe-inline' 'wasm-unsafe-eval' "
    "https://pyscript.net https://cdn.jsdelivr.net "
    "https://cdn.tailwindcss.com; "
    "style-src 'self' 'unsafe-inline' "
    "https://pyscript.net https://fonts.googleapis.com; "
    "font-src 'self' https://fonts.gstatic.com; "
    "img-src 'self' data:; "
    "connect-src 'self' "
    "https://cdn.jsdelivr.net https://pyscript.net"
)


@app.after_request
def _set_preview_csp(response):
    """Set a permissive CSP on /preview so PyScript + WASM + MQTT-WS can run.

    All other pages keep the browser's default CSP (none, since we don't
    set one). Only the preview needs the wasm-unsafe-eval + PyScript CDN
    + MQTT-WS exceptions; everywhere else is unaffected.
    """
    if request.path == "/preview" or request.path.startswith("/preview/"):
        from urllib.parse import urlparse

        mqtt_ws_url = _derive_mqtt_ws_url()
        parsed = urlparse(mqtt_ws_url)
        # CSP `connect-src` matches scheme + host + port (the path is
        # irrelevant). Build the origin string and splice it into the
        # base directive.
        ws_origin = f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme else ""
        csp = _PREVIEW_CSP_BASE
        if ws_origin:
            csp = csp.replace(
                "connect-src 'self'",
                f"connect-src 'self' {ws_origin}",
            )
        response.headers["Content-Security-Policy"] = csp
    return response


# Context processor — inject the `mqtt`, `mqtt_ws`, `config`, and `auth`
# namespaces into every template render so `templates/base.html`'s
# inline `APP_CONFIG` block can pull values from `settings.toml` at
# request time. The browser's MQTT-WS client and the in-browser
# MessageManager seed fetch use the same X-API-Key the device uses.


def _derive_mqtt_ws_url() -> str:
    """Derive the MQTT-over-WebSocket URL from MQTT_HOST if not set explicitly.

    Loopback (`127.0.0.1` / `localhost`) → `ws://<host>:9002/mqtt`.
    Anything else (e.g. `io.adafruit.com`) → `wss://<host>/mqtt` (broker
    default 443). The container's WS port stays 9001 (the protocol
    default), but `scripts/start-app.sh` maps it to host 9002 to avoid
    colliding with MinIO's console (which owns host 9001). Override
    with `MQTT_WS_URL` in settings.toml if your broker is on a
    different port or scheme.

    Default host is `127.0.0.1`, not `localhost`. The TCP connection
    lands at the same loopback either way, but Chromium-based
    browsers with built-in tracker blockers (Arc, in particular) have
    been observed to block `ws://localhost:9002/mqtt` while letting
    `ws://127.0.0.1:9002/mqtt` through — the blocker applies
    heuristics to `localhost` URLs that it doesn't apply to literal
    IPs. Using the IP sidesteps the false positive. Set `MQTT_HOST` in
    settings.toml to override.
    """
    explicit = _cfg.if_exists("MQTT_WS_URL")
    if explicit:
        return explicit
    host = _cfg.if_exists("MQTT_HOST") or "127.0.0.1"
    if host in ("127.0.0.1", "localhost"):
        return f"ws://{host}:9002/mqtt"
    return f"wss://{host}/mqtt"


def _mqtt_long_disconnect_ms() -> int:
    raw = _cfg.if_exists("MQTT_LONG_DISCONNECT_MS")
    if raw is None:
        return 300000
    try:
        return int(raw)
    except ValueError:
        return 300000


@app.context_processor
def _inject_app_config():
    """Inject `mqtt_ws`, `mqtt`, `config`, and `auth` into every template."""
    return {
        "mqtt_ws": {
            "MQTT_WS_URL": _derive_mqtt_ws_url(),
            "MQTT_LONG_DISCONNECT_MS": _mqtt_long_disconnect_ms(),
        },
        "mqtt": {
            "MQTT_USERNAME": _cfg.if_exists("MQTT_USERNAME") or "",
            "MQTT_PASSWORD": _cfg.if_exists("MQTT_PASSWORD") or "",
            # Adafruit IO routes publishes to `username/feeds/<feed>` on the
            # wire (the Adafruit_IO Python client publishes there directly).
            # Subscriptions, however, accept BOTH `username/<feed>` and
            # `username/feeds/<feed>`. The latter is what the broker actually
            # delivers, so we expose that form to the browser — otherwise
            # SUBSCRIBE on `username/<feed>` succeeds (SUBACK 0x01) but the
            # broker never forwards publish frames to that subscription.
            "MQTT_TOPIC": "{}/feeds/{}".format(
                _cfg.if_exists("MQTT_USERNAME") or "",
                _cfg.if_exists("MQTT_TOPIC") or "",
            ),
        },
        "config": {
            "MESSAGES_API_URL": _cfg.if_exists("MESSAGES_API_URL") or "",
            "CONFIG_API_URL": _cfg.if_exists("CONFIG_API_URL") or "",
        },
        "auth": {
            "API_SECRET_KEY": _cfg.if_exists("API_SECRET_KEY") or "",
        },
    }


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(_cfg.PORT), debug=True)
