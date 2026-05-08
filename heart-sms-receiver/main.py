import html
import json
import tomllib
from datetime import datetime
from pathlib import Path

from flask import Flask, Response, jsonify, redirect, render_template, request, url_for
from lib import filters, storage
from lib.models import Config, FilterRule

app = Flask(__name__)

# Load legacy settings.toml for Adafruit IO credentials (used for publish stub)
_settings_path = Path(__file__).parent / "settings.toml"
if _settings_path.exists():
    with open(_settings_path, "rb") as f:
        _cfg = tomllib.load(f)
    AIO_USERNAME = _cfg.get("AIO_USERNAME", "")
    AIO_KEY = _cfg.get("AIO_KEY", "")
    AIO_FEED = _cfg.get("AIO_FEED", "")


# ---------------------------------------------------------------------------
# Config publish stub — called after any config change via admin UI.
# TODO: replace with real MQTT or HTTP publish once ESP32 comms are decided.
# ---------------------------------------------------------------------------


def publish_config(config_json: str) -> None:
    """
    Stub: publish updated config JSON to the ESP32 communication layer.
    Currently a no-op; logs the JSON for visibility.
    """
    app.logger.info("publish_config called with: %s", config_json)


# ---------------------------------------------------------------------------
# Init
# ---------------------------------------------------------------------------

with app.app_context():
    storage.init_db()


# ---------------------------------------------------------------------------
# Twilio webhook
# ---------------------------------------------------------------------------


@app.route("/api/messages", methods=["POST"])
def api_messages():
    """Receive Twilio SMS webhook, store to SQLite, return TwiML."""
    sender = request.form.get("From", "")
    body = request.form.get("Body", "").strip()

    if not body:
        return "", 204

    cfg = storage.get_config()

    # Store message first (per spec: store before responding)
    storage.put_message(sender, body)

    # Enforce allowed_senders: if non-empty and sender not in list, suppress
    if cfg.allowed_senders:
        phones = {s.phone for s in cfg.allowed_senders}
        if sender not in phones:
            # Add sender suppress rule automatically
            rule = FilterRule(type="sender", pattern=sender, action="suppress")
            cfg.filters.append(rule)
            storage.put_config(cfg)
            publish_config(json.dumps(cfg.to_dict()))

    # Build TwiML reply
    sign_name = cfg.sign.name or "Lindsay's Heart"
    reply = f"{sign_name} got your message: {html.escape(body)}"
    return Response(
        f"<Response><Message>{reply}</Message></Response>",
        status=200,
        mimetype="text/xml",
    )


@app.route("/api/messages/<message_id>/suppress", methods=["POST"])
def suppress_message(message_id: str):
    """Add a type=message filter rule to suppress the given message UUID."""
    cfg = storage.get_config()
    # Remove any existing rule for this message
    cfg.filters = [f for f in cfg.filters if not (f.type == "message" and f.pattern == message_id)]
    cfg.filters.append(FilterRule(type="message", pattern=message_id, action="suppress"))
    storage.put_config(cfg)
    publish_config(json.dumps(cfg.to_dict()))
    return "", 204


@app.route("/api/messages/<message_id>/unsuppress", methods=["POST"])
def unsuppress_message(message_id: str):
    """Remove any type=message filter rule for the given message UUID."""
    cfg = storage.get_config()
    cfg.filters = [f for f in cfg.filters if not (f.type == "message" and f.pattern == message_id)]
    storage.put_config(cfg)
    publish_config(json.dumps(cfg.to_dict()))
    return "", 204


# ---------------------------------------------------------------------------
# Config API
# ---------------------------------------------------------------------------


@app.route("/api/config", methods=["GET"])
def get_config():
    """Return current config JSON."""
    cfg = storage.get_config()
    return jsonify(cfg.to_dict())


@app.route("/api/config", methods=["PUT"])
def put_config():
    """Accept full config JSON, persist it, and publish to ESP32."""
    data = request.get_json()
    cfg = Config.from_dict(data)
    storage.put_config(cfg)
    publish_config(json.dumps(cfg.to_dict()))
    return "", 204


# ---------------------------------------------------------------------------
# Message list API
# ---------------------------------------------------------------------------


@app.route("/api/messages", methods=["GET"])
def get_messages():
    """Return messages as JSON. ?since=ISO timestamp filters by received_at."""
    since_str = request.args.get("since")
    if since_str:
        since = datetime.fromisoformat(since_str)
        messages = storage.get_messages_since(since)
    else:
        messages = storage.get_all_messages()
    return jsonify([m.to_dict() for m in messages])


# ---------------------------------------------------------------------------
# Legacy webhook (keeps existing /sms route working for backward compat)
# ---------------------------------------------------------------------------


@app.route("/sms", methods=["POST"])
def sms_webhook():
    """Legacy route — delegates to /api/messages."""
    return api_messages()


# ---------------------------------------------------------------------------
# Admin UI — Dashboard
# ---------------------------------------------------------------------------


@app.route("/", methods=["GET"])
def dashboard():
    recent = storage.get_all_messages()[-20:][::-1]  # 20 most recent, newest first
    cfg = storage.get_config()
    total = len(storage.get_all_messages())

    # Count suppressions per filter type
    filter_counts: dict[str, int] = {}
    all_msgs = storage.get_all_messages()
    for rule in cfg.filters:
        if rule.action == "suppress":
            count = sum(1 for m in all_msgs if filters.apply(m, cfg) and
                        (rule.type == "message" and rule.pattern == m.id or
                         rule.type == "sender" and rule.pattern == m.sender or
                         rule.type == "keyword" and rule.pattern.lower() in m.body.lower() or
                         rule.type == "regex"))
            filter_counts[rule.type] = filter_counts.get(rule.type, 0) + count

    return render_template(
        "dashboard.html",
        recent=recent,
        total=total,
        filter_counts=filter_counts,
    )


# ---------------------------------------------------------------------------
# Admin UI — Message list
# ---------------------------------------------------------------------------


@app.route("/messages", methods=["GET"])
def messages_page():
    page = int(request.args.get("page", 1))
    per_page = 50
    all_msgs = storage.get_all_messages()[::-1]  # newest first
    total_pages = max(1, (len(all_msgs) + per_page - 1) // per_page)
    page_msgs = all_msgs[(page - 1) * per_page : page * per_page]
    cfg = storage.get_config()
    # Pre-compute suppressed status for display
    msg_status = [(m, filters.apply(m, cfg)) for m in page_msgs]
    return render_template(
        "messages.html",
        msg_status=msg_status,
        page=page,
        total_pages=total_pages,
    )


# ---------------------------------------------------------------------------
# Admin UI — Filter rules
# ---------------------------------------------------------------------------


@app.route("/filters", methods=["GET", "POST"])
def filters_page():
    cfg = storage.get_config()
    if request.method == "POST":
        ftype = request.form.get("type")
        pattern = request.form.get("pattern", "").strip()
        if ftype and pattern:
            cfg.filters.append(FilterRule(type=ftype, pattern=pattern, action="suppress"))
            storage.put_config(cfg)
            publish_config(json.dumps(cfg.to_dict()))
            return redirect(url_for("filters_page"))
    return render_template("filters.html", filters=cfg.filters)


@app.route("/filters/<int:index>", methods=["DELETE"])
def delete_filter(index: int):
    """Delete a filter rule by its index in the list."""
    cfg = storage.get_config()
    if 0 <= index < len(cfg.filters):
        del cfg.filters[index]
        storage.put_config(cfg)
        publish_config(json.dumps(cfg.to_dict()))
    return "", 204


# ---------------------------------------------------------------------------
# Admin UI — Settings
# ---------------------------------------------------------------------------


@app.route("/settings", methods=["GET", "POST"])
def settings_page():
    cfg = storage.get_config()
    if request.method == "POST":
        # Update sign name
        cfg.sign.name = request.form.get("sign_name", cfg.sign.name)
        # Update rendering
        cfg.rendering.mode = request.form.get("rendering_mode", cfg.rendering.mode)
        cfg.rendering.speed = float(request.form.get("rendering_speed", cfg.rendering.speed))
        cfg.rendering.color = int(request.form.get("rendering_color", cfg.rendering.color))
        # Update allowed senders
        names = request.form.getlist("sender_name")
        phones = request.form.getlist("sender_phone")
        cfg.allowed_senders = [
            AllowedSender(name=n, phone=p)
            for n, p in zip(names, phones) if p.strip()
        ]
        storage.put_config(cfg)
        publish_config(json.dumps(cfg.to_dict()))
        return redirect(url_for("settings_page"))
    return render_template(
        "settings.html",
        config=cfg,
        rendering_modes=["scroll", "static"],
    )


# ---------------------------------------------------------------------------
# Admin UI — Preview
# ---------------------------------------------------------------------------


@app.route("/preview", methods=["GET"])
def preview_page():
    all_msgs = storage.get_all_messages()
    cfg = storage.get_config()
    visible = filters.display_list(all_msgs, cfg)
    return render_template("preview.html", messages=visible, sign_name=cfg.sign.name)


# ---------------------------------------------------------------------------
# AllowedSender import for settings
# ---------------------------------------------------------------------------
from lib.models import AllowedSender


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
