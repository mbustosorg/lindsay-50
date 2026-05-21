"""Flask app for heart-message-manager.

Handles Twilio webhooks, stores messages to SQLite (with S3 backup),
publishes to Adafruit IO, and serves the admin UI.
"""

import html
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
import sys

from flask import Flask, Response, jsonify, redirect, render_template, request, url_for

sys.path.insert(0, str(Path(__file__).parent.parent))

from Adafruit_IO import Client
import tomllib

from lib import storage, s3, publish
from lib_shared.models import Config, FilterRule, Message
from lib_shared.subscribe import MessagesSubscriber


# App setup
app = Flask(__name__)
_secret_key_path = Path(__file__).parent / "secret_key"
if _secret_key_path.exists():
    app.secret_key = _secret_key_path.read_text()
else:
    app.secret_key = uuid.uuid4().hex
    _secret_key_path.write_text(str(app.secret_key))

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

SERVER_PORT = _cfg.get("PORT", 3100)


# Start the Adafruit client
aio = Client(AIO_USERNAME, AIO_KEY)


# Wipe SQLite and rebuild messages and config from S3 on startup
_db_path = Path(__file__).parent / "db.sqlite"
if _db_path.exists():
    _db_path.unlink()
    logger.info("Wiped existing SQLite database")

storage.init_db()

try:
    storage.rebuild_from_s3(s3.load_messages_from_s3)
    logger.info("Rebuilt messages from S3")
except Exception as e:
    logger.warning("Could not rebuild messages from S3 (S3 may not be configured): %s", e)

try:
    latest = s3.load_latest_config()
    if latest:
        cfg = Config.from_dict(latest)
        storage.put_config(cfg)
        logger.info("Loaded config from S3 snapshot")
except Exception as e:
    logger.warning("Could not load config from S3 (S3 may not be configured): %s", e)


# Print environment and config for debugging
logger.info("=== DEBUG CONFIG ===")
logger.info("OBJC_DISABLE_FORK_SAFETY=%s", __import__("os").environ.get("OBJC_DISABLE_FORK_SAFETY", "NOT SET"))
logger.info("MQTT_HOST=%s", _cfg.get("MQTT_HOST", "NOT SET"))
logger.info("MQTT_PORT=%s", _cfg.get("MQTT_PORT", "NOT SET"))
logger.info("AIO_FEED=%s", AIO_FEED)
logger.info("SERVER_PORT=%s", SERVER_PORT)
logger.info("=== END DEBUG CONFIG ===")


# MessagesSubscriber — starts MQTT threads at worker boot
_messages_sub: MessagesSubscriber = MessagesSubscriber(
    feed=AIO_FEED,
    config_feed=_cfg.get("AIO_CONFIG_FEED", "config"),
    api_url=f"http://localhost:{SERVER_PORT}/api/messages",
    config_api_url=f"http://localhost:{SERVER_PORT}/api/config",
)
logger.info("Starting MessagesSubscriber at boot...")
_messages_sub.start()


# Inbound Twilio webhook handler
@app.route("/api/messages", methods=["POST"])
def api_messages():
    """Receive Twilio webhook: log to S3 → respond → store → publish."""
    sender = request.form.get("From", "")
    body = request.form.get("Body", "").strip()

    logger.info("From=%r Body=%r", sender, body)

    if not body:
        return Response("", status=204)

    msg = Message(
        id=str(uuid.uuid4()),
        sender=sender,
        body=body,
        received_at=_now_iso(),
    )

    try:
        s3.log_message(msg)
    except Exception as e:
        logger.warning("S3 logging failed (will continue): %s", e)

    cfg = storage.get_config()
    sign_name = cfg.sign.name if cfg.sign else "Lindsay's Heart"
    reply = f"{sign_name} got your message: {html.escape(body)}"
    twiml = Response(
        f"<Response><Message>{reply}</Message></Response>",
        status=200,
        mimetype="text/xml",
    )

    try:
        storage.put_message(msg)
        publish.publish_message(
            body=body,
            msg_id=msg.id,
            sender=msg.sender,
            received_at=msg.received_at,
        )
    except Exception as e:
        logger.error("Post-webhook processing failed: %s", e)

    return twiml


# API Endpoints
@app.route("/api/messages", methods=["GET"])
def api_get_messages():
    """GET /api/messages?since=<timestamp> — return messages as JSON."""
    since = request.args.get("since")
    if since:
        msgs = storage.get_messages_since(since)
    else:
        msgs = storage.get_all_messages()
    return jsonify([m.to_dict() for m in msgs])


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


@app.route("/messages/<msg_id>/suppress", methods=["POST"])
def web_suppress(msg_id):
    """Web-form wrapper for suppress that redirects back to the messages page."""
    _suppress_message(msg_id)
    return redirect(url_for("message_list"))


@app.route("/messages/<msg_id>/unsuppress", methods=["POST"])
def web_unsuppress(msg_id):
    """Web-form wrapper for unsuppress that redirects back to the messages page."""
    _unsuppress_message(msg_id)
    return redirect(url_for("message_list"))


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


# Testing API into MessagesSubscriber
@app.route("/api/live-messages", methods=["GET"])
def api_live_messages():
    """Return messages in the live ring buffer, newest first."""
    assert _messages_sub is not None
    limit = min(100, int(request.args.get("limit", 100)))
    return jsonify([m.to_dict() for m in _messages_sub.get_messages(limit=limit)])


@app.route("/api/live-messages/seed", methods=["POST"])
def api_live_messages_seed():
    """Back-populate the live message ring buffer from a REST call."""
    assert _messages_sub is not None
    _messages_sub.seed()
    return jsonify({"status": "ok", "seeded": min(50, len(storage.get_all_messages()))})


@app.route("/api/live-config", methods=["GET"])
def api_live_config():
    """Return the subscriber's current config buffer."""
    assert _messages_sub is not None
    return jsonify(_messages_sub.config.to_dict())


@app.route("/api/admin/s3-objects")
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
            nodes.append({
                "id": folder_id,
                "text": name,
                "children": True,
            })

        for obj in response.get("Contents", []):
            key = obj["Key"]
            name = key.split("/")[-1]
            size = obj["Size"]
            size_str = str(size) if size < 1024 else f"{size / 1024:.1f}KB"
            nodes.append({
                "id": key,
                "text": f"{name} ({size_str})",
                "children": False,
            })

        # Sort descending by key (chronological, newest first)
        nodes.sort(key=lambda n: n["id"], reverse=True)

        return jsonify({"bucket": bucket, "prefix": prefix, "nodes": nodes})
    except Exception as e:
        logger.warning("S3 list failed: %s", e)
        err_str = str(e)
        if "NoSuchBucket" in err_str:
            return jsonify({"error": "Bucket does not exist. Create it in MinIO console."}), 500
        return jsonify({"error": err_str}), 500


@app.route("/api/admin/s3-object")
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
def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _save_and_publish(cfg: Config) -> None:
    """Save config to SQLite, snapshot S3, publish to Adafruit IO."""
    storage.put_config(cfg)
    try:
        s3.save_config_snapshot(cfg.to_dict())
    except Exception as e:
        logger.warning("Config S3 snapshot failed: %s", e)
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


# UI Routes
@app.route("/")
def dashboard():
    """Dashboard: recent messages and counts."""
    msgs = storage.get_all_messages()[:20]
    cfg = storage.get_config()
    total = storage.message_count()

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
    )


@app.route("/messages")
def message_list():
    """Paginated message list with suppress/unsuppress buttons."""
    page = max(1, int(request.args.get("page", 1)))
    per_page = 50

    all_msgs = storage.get_all_messages()
    total = len(all_msgs)
    total_pages = max(1, (total + per_page - 1) // per_page)

    start = (page - 1) * per_page
    end = start + per_page
    page_msgs = all_msgs[start:end]

    cfg = storage.get_config()

    return render_template(
        "messages.html",
        messages=page_msgs,
        page=page,
        total_pages=total_pages,
        cfg=cfg,
        sign_name=cfg.sign.name if cfg.sign else "Lindsay's Heart",
    )


@app.route("/filters")
def filter_rules():
    """Redirect old filter_rules URL to settings."""
    return redirect(url_for("settings"))


@app.route("/settings", methods=["GET", "POST"])
def settings():
    """Allowed senders, rendering defaults, sign name, and filter rules."""
    cfg = storage.get_config()

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
def preview():
    """Preview: show what display_list() returns, with toggle for include_filtered."""
    include_filtered = request.args.get("include_filtered", "false") == "true"
    cfg = storage.get_config()
    all_msgs = storage.get_all_messages()

    return render_template(
        "preview.html",
        result=all_msgs,
        include_filtered=include_filtered,
        sign_name=cfg.sign.name if cfg.sign else "Lindsay's Heart",
        timezone=cfg.timezone,
    )


@app.route("/testing")
def testing():
    """Testing: inject messages and inspect system state."""
    cfg = storage.get_config()
    return render_template("testing.html", sign_name=cfg.sign.name if cfg.sign else "Lindsay's Heart")


@app.route("/health")
def health():
    return "ok"


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=SERVER_PORT, debug=True)
