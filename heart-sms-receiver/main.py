"""Flask app for heart-sms-receiver.

Handles Twilio webhooks, stores messages to SQLite (with S3 backup),
publishes to Adafruit IO, and serves the admin UI.
"""

import html
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, Response, jsonify, redirect, render_template, request, url_for

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

import tomllib

from lib import filters, storage, s3, publish
from lib.models import Config, FilterRule, Message, AllowedSender

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

app = Flask(__name__)
app.secret_key = Path(__file__).parent / "secret_key"
if not app.secret_key.exists():
    app.secret_key.write_text(uuid.uuid4().hex)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Load settings
_settings_path = Path(__file__).parent / "settings.toml"
if not _settings_path.exists():
    raise RuntimeError("settings.toml not found; copy settings.toml.example first")
with open(_settings_path, "rb") as f:
    _cfg = tomllib.load(f)

AIO_USERNAME = _cfg["AIO_USERNAME"]
AIO_KEY = _cfg["AIO_KEY"]
AIO_FEED = _cfg["AIO_FEED"]
AIO_CONFIG_FEED = _cfg.get("AIO_CONFIG_FEED", "")

# ---------------------------------------------------------------------------
# Startup connectivity checks
# ---------------------------------------------------------------------------

def _check_connectivity() -> None:
    """Verify MQTT broker and S3 are reachable. Logs warnings on failure."""
    import boto3

    # MQTT: try to connect to the broker
    try:
        import paho.mqtt.client as mqtt
        cfg = _cfg
        host = cfg.get("MQTT_HOST", "io.adafruit.com")
        port = int(cfg.get("MQTT_PORT", 8883))
        client = mqtt.Client()
        username = cfg.get("MQTT_USERNAME")
        if username:
            client.username_pw_set(username, cfg.get("MQTT_PASSWORD"))
        client.connect(host, port, keepalive=5)
        client.disconnect()
        logger.info("MQTT connectivity OK (%s:%d)", host, port)
    except Exception as e:
        logger.warning("MQTT unreachable: %s (messages will not be published)", e)

    # S3: try head_bucket to verify bucket + credentials
    try:
        bucket = _cfg.get("S3_BUCKET", "")
        if bucket:
            client = s3._s3_client()
            client.head_bucket(Bucket=bucket)
            logger.info("S3 connectivity OK (bucket: %s)", bucket)
        else:
            logger.warning("S3_BUCKET not set; S3 logging disabled")
    except Exception as e:
        logger.warning("S3 unreachable: %s (messages will not be logged to S3)", e)


# ---------------------------------------------------------------------------
# Startup: init SQLite and rebuild from S3
# ---------------------------------------------------------------------------

storage.init_db()
_check_connectivity()
publish.start_mqtt_subscriber()

# Rebuild SQLite from S3 on startup
try:
    storage.rebuild_from_s3(s3.load_messages_from_s3)
    logger.info("Rebuilt SQLite from S3")
except Exception as e:
    logger.warning("Could not rebuild from S3 (S3 may not be configured): %s", e)

# Load latest config from S3 if SQLite is empty
cfg = storage.get_config()
if cfg.version == 1 and not any(f.type for f in cfg.filters):
    latest = None
    try:
        latest = s3.load_latest_config()
    except Exception:
        pass
    if latest:
        cfg = Config.from_dict(latest)
        storage.put_config(cfg)
        logger.info("Loaded config from S3 snapshot")


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sender_name(phone: str, cfg: Config) -> str | None:
    for s in cfg.allowed_senders:
        if s.phone == phone:
            return s.name
    return None


# ---------------------------------------------------------------------------
# Twilio webhook
# ---------------------------------------------------------------------------

@app.route("/api/messages", methods=["POST"])
def api_messages():
    """Receive Twilio webhook: log to S3 → respond → store → publish."""
    sender = request.form.get("From", "")
    body = request.form.get("Body", "").strip()

    logger.info("From=%r Body=%r", sender, body)

    if not body:
        return Response("", status=204)

    # Build message
    msg = Message(
        id=str(uuid.uuid4()),
        sender=sender,
        body=body,
        received_at=_now_iso(),
    )

    # Get current config for allowed_senders lookup
    cfg = storage.get_config()
    sname = _sender_name(sender, cfg)

    # 1. Log to S3 BEFORE responding (source of truth)
    try:
        s3.log_message(msg, sender_name=sname)
    except Exception as e:
        logger.warning("S3 logging failed (will continue): %s", e)

    # 2. Respond to Twilio immediately
    sign_name = cfg.sign.name if cfg.sign else "Lindsay's Heart"
    reply = f"{sign_name} got your message: {html.escape(body)}"
    twiml = Response(
        f"<Response><Message>{reply}</Message></Response>",
        status=200,
        mimetype="text/xml",
    )

    # Return response first, then do background work
    # We return the response but the caller won't see this due to how
    # we're structured (synchronous for simplicity)
    try:
        # 3. Store to SQLite
        storage.put_message(msg)

        # 4. Publish to Adafruit IO via MQTT
        publish.publish_message(body)
    except Exception as e:
        logger.error("Post-webhook processing failed: %s", e)

    return twiml


# ---------------------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------------------

@app.route("/api/messages", methods=["GET"])
def api_get_messages():
    """GET /api/messages?since=<timestamp> — return all messages with suppression info."""
    cfg = storage.get_config()
    since = request.args.get("since")
    if since:
        all_msgs = storage.get_messages_since(since)
    else:
        all_msgs = storage.get_all_messages()
    # Return all messages with suppression metadata (ESP32 applies filters client-side)
    raw = filters.get_messages(all_msgs, cfg, include_filtered=True)
    # Serialize Message objects to dicts
    result = []
    for entry in raw:
        item = {
            "message": entry["message"].to_dict(),
            "suppressed": entry["suppressed"],
        }
        if entry.get("rule"):
            item["rule"] = entry["rule"]
        result.append(item)
    return jsonify(result)


@app.route("/api/messages/<msg_id>/suppress", methods=["POST"])
def api_suppress(msg_id):
    """Add a type=message filter rule to suppress the given message (JSON API)."""
    msg = storage.get_message(msg_id)
    if msg is None:
        return jsonify({"error": "message not found"}), 404

    added = _suppress_message(msg_id)
    if not added:
        return jsonify({"status": "already suppressed"})
    return jsonify({"status": "ok", "filter_added": {"type": "message", "pattern": msg_id, "action": "suppress"}})


@app.route("/api/messages/<msg_id>/unsuppress", methods=["POST"])
def api_unsuppress(msg_id):
    """Remove type=message filter rule for the given message (JSON API)."""
    removed = _unsuppress_message(msg_id)
    if not removed:
        return jsonify({"status": "not found"}), 404
    return jsonify({"status": "ok"})


@app.route("/api/config", methods=["GET"])
def api_get_config():
    """Return current config as JSON."""
    cfg = storage.get_config()
    return jsonify(cfg.to_dict())


@app.route("/api/config", methods=["PUT"])
def api_put_config():
    """Accept full config JSON, store to SQLite, snapshot S3, publish to Adafruit IO."""
    try:
        data = request.get_json()
    except Exception:
        return jsonify({"error": "invalid JSON"}), 400

    if not isinstance(data, dict):
        return jsonify({"error": "expected JSON object"}), 400

    cfg = Config.from_dict(data)
    _save_and_publish(cfg)
    return jsonify({"status": "ok"})


@app.route("/api/live-messages", methods=["GET"])
def api_live_messages():
    """Return messages received by the MQTT subscriber, newest first.

    This shows what actually flowed through the MQTT broker, not just what
    Flask published — any publisher to the topic will be captured.
    """
    limit = min(100, int(request.args.get("limit", 20)))
    return jsonify(publish.get_live_messages(limit=limit))


@app.route("/api/live-messages/seed", methods=["POST"])
def api_live_messages_seed():
    """Back-populate the live message ring buffer from SQLite messages.

    Simulates what MQTT would have delivered by injecting all stored messages
    into the ring buffer with source="rest". Subsequent real MQTT messages
    will appear as source="mqtt".
    """
    msgs = storage.get_all_messages()
    username = _cfg.get("AIO_USERNAME", "")
    feed = _cfg.get("AIO_FEED", "")
    topic = f"{username}/feeds/{feed}" if username and feed else "unknown/feeds/unknown"
    # Only seed the most recent 50 to avoid flooding the buffer
    publish.seed_from_rest_messages([m.to_dict() for m in msgs[:50]], topic)
    return jsonify({"status": "ok", "seeded": min(50, len(msgs))})


# ---------------------------------------------------------------------------
# Web-level suppress/unsuppress (redirects back to message list)
# ---------------------------------------------------------------------------

@app.route("/messages/<msg_id>/suppress", methods=["POST"])
def web_suppress(msg_id):
    """Web form handler for suppressing a message."""
    _suppress_message(msg_id)
    return redirect(url_for("message_list"))


@app.route("/messages/<msg_id>/unsuppress", methods=["POST"])
def web_unsuppress(msg_id):
    """Web form handler for unsuppressing a message."""
    _unsuppress_message(msg_id)
    return redirect(url_for("message_list"))


# ---------------------------------------------------------------------------
# Admin UI helpers
# ---------------------------------------------------------------------------

def _save_and_publish(cfg: Config) -> None:
    """Save config to SQLite, snapshot S3, publish to Adafruit IO."""
    storage.put_config(cfg)
    try:
        s3.save_config_snapshot(cfg.to_dict())
    except Exception as e:
        logger.warning("Config S3 snapshot failed: %s", e)
    if AIO_CONFIG_FEED:
        publish.publish_config(cfg.to_dict())


def _suppress_message(msg_id: str) -> bool:
    """Add type=message filter rule. Returns True if newly added."""
    msg = storage.get_message(msg_id)
    if msg is None:
        return False
    cfg = storage.get_config()
    for f in cfg.filters:
        if f.type == "message" and f.pattern == msg_id:
            return False
    cfg.filters.append(FilterRule(type="message", pattern=msg_id, action="suppress"))
    _save_and_publish(cfg)
    return True


def _unsuppress_message(msg_id: str) -> bool:
    """Remove type=message filter rule. Returns True if found and removed."""
    cfg = storage.get_config()
    original_len = len(cfg.filters)
    cfg.filters = [f for f in cfg.filters if not (f.type == "message" and f.pattern == msg_id)]
    if len(cfg.filters) == original_len:
        return False
    _save_and_publish(cfg)
    return True


# ---------------------------------------------------------------------------
# Admin UI
# ---------------------------------------------------------------------------

@app.route("/")
def dashboard():
    """Dashboard: recent messages and counts."""
    msgs = storage.get_all_messages()[:20]
    cfg = storage.get_config()
    total = storage.message_count()

    # Count suppressions per filter type
    suppression_counts = {}
    for f in cfg.filters:
        suppression_counts[f.type] = suppression_counts.get(f.type, 0) + 1

    return render_template(
        "dashboard.html",
        messages=msgs[:20],
        total_count=total,
        suppression_counts=suppression_counts,
        sign_name=cfg.sign.name if cfg.sign else "Lindsay's Heart",
    )


@app.route("/messages")
def message_list():
    """Paginated message list with suppress/unsuppress buttons."""
    page = max(1, int(request.args.get("page", 1)))
    per_page = 50
    hide_suppressed = request.args.get("hide_suppressed") == "1"

    all_msgs = storage.get_all_messages()
    cfg = storage.get_config()

    # Build sender name lookup: phone -> name
    sender_name_map: dict[str, str] = {}
    for s in cfg.allowed_senders:
        sender_name_map[s.phone] = s.name

    # Annotate each message with suppression reason and sender name
    # (applies keyword/regex/sender/message rules correctly per-message)
    annotated = []
    for m in all_msgs:
        suppressed, rule = filters.apply(m, cfg)
        if suppressed and rule:
            reason = f"{rule.type}:{rule.pattern}"
        elif suppressed:
            reason = "suppressed"
        else:
            reason = None

        # Filter if hide_suppressed is enabled
        if hide_suppressed and suppressed:
            continue

        annotated.append({
            "msg": m,
            "suppressed_by": reason,
            "sender_name": sender_name_map.get(m.sender),
        })

    total = len(annotated)
    total_pages = max(1, (total + per_page - 1) // per_page)

    start = (page - 1) * per_page
    end = start + per_page
    page_annotated = annotated[start:end]

    return render_template(
        "messages.html",
        messages=page_annotated,
        page=page,
        total_pages=total_pages,
        cfg=cfg,
        hide_suppressed=hide_suppressed,
    )


@app.route("/filters", methods=["GET", "POST"])
def filter_rules():
    """List, add, and delete filter rules."""
    cfg = storage.get_config()

    if request.method == "POST":
        action = request.form.get("action")
        if action == "add":
            ftype = request.form.get("type")
            pattern = request.form.get("pattern", "").strip()
            if ftype in ("keyword", "regex", "sender", "message") and pattern:
                cfg.filters.append(FilterRule(type=ftype, pattern=pattern, action="suppress"))
                _save_and_publish(cfg)
        elif action == "delete":
            idx = int(request.form.get("index", -1))
            if 0 <= idx < len(cfg.filters):
                cfg.filters.pop(idx)
                _save_and_publish(cfg)
        return redirect(url_for("filter_rules"))

    return render_template("filters.html", filters=cfg.filters)


@app.route("/settings", methods=["GET", "POST"])
def settings():
    """Allowed senders, rendering defaults, sign name."""
    cfg = storage.get_config()

    if request.method == "POST":
        # Sign name
        sign_name = request.form.get("sign_name", "").strip()
        if sign_name:
            cfg.sign.name = sign_name

        # Rendering
        cfg.rendering.mode = request.form.get("rendering_mode", cfg.rendering.mode)
        try:
            cfg.rendering.speed = float(request.form.get("rendering_speed", cfg.rendering.speed))
        except ValueError:
            pass
        try:
            cfg.rendering.color = int(request.form.get("rendering_color", cfg.rendering.color), 0)
        except ValueError:
            pass

        # Allowed senders — replace list
        new_senders = []
        names = request.form.getlist("sender_name")
        phones = request.form.getlist("sender_phone")
        for name, phone in zip(names, phones):
            name = name.strip()
            phone = phone.strip()
            if phone:
                new_senders.append(AllowedSender(name=name or phone, phone=phone))
        cfg.allowed_senders = new_senders

        _save_and_publish(cfg)
        return redirect(url_for("settings"))

    return render_template(
        "settings.html",
        cfg=cfg,
    )


@app.route("/testing")
def testing():
    """Testing page: inject test messages and monitor live feed + config."""
    return render_template("testing.html")


# ---------------------------------------------------------------------------
# Health check (for Render)
# ---------------------------------------------------------------------------

@app.route("/health")
def health():
    return "ok"


if __name__ == "__main__":
    import os
    # Respect FLASK_DEBUG env var so reloader is controlled externally.
    debug = os.environ.get("FLASK_DEBUG", "0") != "0"
    port = int(os.environ.get("FLASK_RUN_PORT", 5001))
    app.run(host="0.0.0.0", port=port, debug=debug)
