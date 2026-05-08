import html
import sys
import tomllib
from pathlib import Path

# Add project root to path so lib/ can be imported
sys.path.insert(0, str(Path(__file__).parent.parent))

from flask import Flask, request, Response, jsonify, render_template, redirect, url_for

from lib import init_db, put_message, get_messages_since, get_all_messages, put_config, get_config
from lib.models import Message, Config, FilterRule, AllowedSender
from lib.filters import display_list

app = Flask(__name__)

with open(Path(__file__).parent / "settings.toml", "rb") as f:
    _cfg = tomllib.load(f)

AIO_USERNAME = _cfg["AIO_USERNAME"]
AIO_KEY = _cfg["AIO_KEY"]
AIO_FEED = _cfg["AIO_FEED"]

ALLOWED_SENDERS = [s.strip() for s in _cfg.get("ALLOWED_SENDERS", "").split(",") if s.strip()]

# Initialize database on startup
init_db()


@app.route("/sms", methods=["POST"])
def sms_webhook():
    """Legacy endpoint for backward compatibility with existing Twilio webhook URL."""
    sender = request.form.get("From", "")
    body = request.form.get("Body", "").strip()

    app.logger.info("From=%r Body=%r", sender, body)

    if ALLOWED_SENDERS and sender not in ALLOWED_SENDERS:
        app.logger.warning("Rejected SMS from %s", sender)
        return Response("Forbidden", status=403)

    if not body:
        return Response("", status=204)

    message = Message.create(sender=sender, body=body)
    put_message(message)

    try:
        from adafruit_io import Client
        aio = Client(AIO_USERNAME, AIO_KEY)
        aio.send_data(AIO_FEED, body)
        app.logger.info("Published to feed %s: %s", AIO_FEED, body)
    except Exception as e:
        app.logger.error("Adafruit IO publish failed: %s", e)

    reply = f"Lindsay's Heart got your message: {html.escape(body)}"
    return Response(
        f"<Response><Message>{reply}</Message></Response>",
        status=200,
        mimetype="text/xml",
    )


# === API Endpoints ===

@app.route("/api/messages", methods=["POST"])
def api_post_message():
    """Twilio webhook: receives SMS, stores to SQLite, returns TwiML."""
    sender = request.form.get("From", "")
    body = request.form.get("Body", "").strip()
    to = request.form.get("To", "")

    app.logger.info("API: From=%r Body=%r To=%r", sender, body, to)

    config = get_config()

    # Check allowed senders
    if config.allowed_senders:
        sender_phones = [s.phone for s in config.allowed_senders]
        if sender not in sender_phones:
            # Auto-suppress this sender
            config.filters.append(FilterRule(type="sender", pattern=sender, action="suppress"))
            put_config(config)
            app.logger.warning("Auto-suppressed sender %s", sender)

    if not body:
        return Response("", status=204)

    message = Message.create(sender=sender, body=body)
    put_message(message)

    reply = f"Lindsay's Heart got your message: {html.escape(body)}"
    return Response(
        f"<Response><Message>{reply}</Message></Response>",
        status=200,
        mimetype="text/xml",
    )


@app.route("/api/messages", methods=["GET"])
def api_get_messages():
    """Long-poll for new messages. Returns all messages with received_at strictly after 'since'."""
    since = request.args.get("since")
    messages = get_messages_since(since)
    return jsonify({
        "messages": [
            {
                "id": m.id,
                "sender": m.sender,
                "body": m.body,
                "received_at": m.received_at,
            }
            for m in messages
        ]
    })


@app.route("/api/config", methods=["GET"])
def api_get_config():
    """Return current config JSON."""
    config = get_config()
    return jsonify(config.to_dict())


@app.route("/api/config", methods=["PUT"])
def api_put_config():
    """Update config JSON."""
    data = request.get_json()
    if data is None:
        return jsonify({"error": "Invalid JSON"}), 400
    config = Config.from_dict(data)
    put_config(config)
    return jsonify({"ok": True})


@app.route("/api/messages/<message_id>/suppress", methods=["POST"])
def api_suppress_message(message_id: str):
    """Add a type=message filter rule for the given message UUID."""
    config = get_config()
    # Check if already suppressed
    for f in config.filters:
        if f.type == "message" and f.pattern == message_id:
            return jsonify({"ok": True})  # Already suppressed

    config.filters.append(FilterRule(type="message", pattern=message_id, action="suppress"))
    put_config(config)
    return jsonify({"ok": True})


@app.route("/api/messages/<message_id>/unsuppress", methods=["POST"])
def api_unsuppress_message(message_id: str):
    """Remove the type=message filter rule for the given message UUID."""
    config = get_config()
    config.filters = [f for f in config.filters if not (f.type == "message" and f.pattern == message_id)]
    put_config(config)
    return jsonify({"ok": True})


# === Admin UI Actions ===

@app.route("/messages/<message_id>/suppress", methods=["POST"])
def suppress_message(message_id: str):
    """Admin UI action to suppress a message."""
    config = get_config()
    for f in config.filters:
        if f.type == "message" and f.pattern == message_id:
            pass  # Already suppressed
    else:
        config.filters.append(FilterRule(type="message", pattern=message_id, action="suppress"))
        put_config(config)
    page = int(request.form.get("page", 1))
    return redirect(url_for("messages_page", page=page))


@app.route("/messages/<message_id>/unsuppress", methods=["POST"])
def unsuppress_message(message_id: str):
    """Admin UI action to unsuppress a message."""
    config = get_config()
    config.filters = [f for f in config.filters if not (f.type == "message" and f.pattern == message_id)]
    put_config(config)
    page = int(request.form.get("page", 1))
    return redirect(url_for("messages_page", page=page))


# === Admin UI ===

@app.route("/")
def dashboard():
    """Dashboard: recent messages and counts."""
    messages = get_all_messages()
    config = get_config()

    # Calculate counts
    total = len(messages)
    suppressed_by_sender = sum(1 for f in config.filters if f.type == "sender")
    suppressed_by_keyword = sum(1 for f in config.filters if f.type == "keyword")
    suppressed_by_regex = sum(1 for f in config.filters if f.type == "regex")
    suppressed_by_message = sum(1 for f in config.filters if f.type == "message")
    displayed = len(display_list(messages, config))

    # Recent messages (last 20)
    recent = sorted(messages, key=lambda m: m.received_at, reverse=True)[:20]

    return render_template(
        "dashboard.html",
        recent=recent,
        total=total,
        displayed=displayed,
        suppressed_by_sender=suppressed_by_sender,
        suppressed_by_keyword=suppressed_by_keyword,
        suppressed_by_regex=suppressed_by_regex,
        suppressed_by_message=suppressed_by_message,
        config=config,
    )


@app.route("/messages")
def messages_page():
    """Paginated message list."""
    page = int(request.args.get("page", 1))
    per_page = 50

    messages = sorted(get_all_messages(), key=lambda m: m.received_at, reverse=True)
    total = len(messages)
    total_pages = (total + per_page - 1) // per_page

    start = (page - 1) * per_page
    end = start + per_page
    page_messages = messages[start:end]

    return render_template(
        "messages.html",
        messages=page_messages,
        page=page,
        total_pages=total_pages,
        total=total,
    )


@app.route("/filters", methods=["GET", "POST"])
def filters_page():
    """List, add, and delete filter rules."""
    config = get_config()

    if request.method == "POST":
        action = request.form.get("action")
        if action == "add":
            filter_type = request.form.get("type")
            pattern = request.form.get("pattern")
            if filter_type and pattern:
                config.filters.append(FilterRule(type=filter_type, pattern=pattern, action="suppress"))
                put_config(config)
        elif action == "delete":
            index = int(request.form.get("index", -1))
            if 0 <= index < len(config.filters):
                del config.filters[index]
                put_config(config)
        return redirect(url_for("filters_page"))

    return render_template("filters.html", filters=config.filters)


@app.route("/settings", methods=["GET", "POST"])
def settings_page():
    """Settings: allowed_senders, rendering defaults, sign name."""
    config = get_config()

    if request.method == "POST":
        # Update sign name
        sign_name = request.form.get("sign_name", "").strip()
        if sign_name:
            config.sign.name = sign_name

        # Update rendering
        config.rendering.mode = request.form.get("rendering_mode", "scroll")
        try:
            config.rendering.speed = float(request.form.get("rendering_speed", "0.04"))
        except ValueError:
            pass
        try:
            config.rendering.color = int(request.form.get("rendering_color", "16711680"), 0)
        except ValueError:
            pass

        # Handle allowed_senders
        names = request.form.getlist("sender_name")
        phones = request.form.getlist("sender_phone")
        config.allowed_senders = []
        for name, phone in zip(names, phones):
            name = name.strip()
            phone = phone.strip()
            if name and phone:
                config.allowed_senders.append(AllowedSender(name=name, phone=phone))

        put_config(config)
        return redirect(url_for("settings_page"))

    return render_template("settings.html", config=config)


@app.route("/preview")
def preview_page():
    """Preview: shows what the ESP32 will display."""
    messages = get_all_messages()
    config = get_config()
    display = display_list(messages, config)
    return render_template("preview.html", messages=display, config=config)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
