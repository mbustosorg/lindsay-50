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
from lib_shared.boot_config import BootConfig, from_heroku_or_git as _boot_config_from_heroku_or_git
from lib_shared.config_migrations import migrate, migrate_on_startup
from lib_shared.models import SignConfig, FilterRule, Message
from lib_shared.models import MessageEnvelope
from lib_shared.models import _DEFAULT_EFFECTS_LIST_FULL
from lib_shared.scroller_base import ScrollerBase

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


_mqtt_client = PahoMqttClient(
    dispatch_callback=_noop_dispatch,
    host=_cfg.MQTT_HOST,
    port=_cfg.MQTT_PORT,
    username=_cfg.MQTT_USERNAME,
    password=_cfg.MQTT_PASSWORD,
    topic=_cfg.MQTT_TOPIC,
)
logger.info("Starting MQTT client at boot...")
_mqtt_client.start()


# One-shot check-for-update hint. Published exactly once per Flask
# process lifetime, immediately after the MQTT subscriber starts.
# The Pi's existing MessageManager subscriber receives this and
# invokes its registered `check_for_update` handler, which queries
# /api/sign/boot-config and execs into the loader if the expected
# SHA differs from the running one.
#
# Published via the live `_mqtt_client` (not the once-per-process
# `publish_envelope` short-lived client) so we don't double the
# connection setup. paho queues the publish until the CONNACK
# arrives — if the broker is unreachable at this moment the message
# is held by paho and delivered on the next connect, which is
# exactly the resilience we want. Reconnects DO NOT republish
# (that's the v1 fix — `on_connect_callback` is gone).
def _publish_check_for_update_once() -> None:
    """Publish `command=check-for-update` once at Flask startup.

    Runs synchronously after `_mqtt_client.start()`. paho queues
    the publish until CONNACK. Failures are logged, never raised:
    a missing hint is recoverable (the next Flask boot will hint
    again, the Pi's loader re-checks on every boot).
    """
    try:
        envelope = MessageEnvelope("command", {"action": "check-for-update"})
        ok = _mqtt_client.publish_envelope(envelope)
        logger.info(
            "[flask] _publish_check_for_update_once: publish_envelope returned %s",
            ok,
        )
    except Exception as e:
        logger.warning("[flask] _publish_check_for_update_once raised: %s", e)


_publish_check_for_update_once()


def _mqtt_client_publish_config(cfg_dict: dict) -> None:
    """Publish a config dict as a `type="config"` envelope to MQTT.

    Defined here (before the `migrate_on_startup` call) so the migration's
    `mqtt_publisher` lambda can forward to it on the fresh-install path
    (which fires the publisher before the rest of the file is loaded).
    """
    assert _mqtt_client is not None
    ok = _mqtt_client.publish_envelope(MessageEnvelope("config", cfg_dict))
    logger.info("[flask] _mqtt_client_publish_config: publish_envelope returned %s", ok)


# Run the config migration on startup: read the latest S3 config, migrate to
# CURRENT_VERSION if needed, and write back to SQLite, MQTT, and S3. The
# running code only ever sees the current version after this runs once.
# Exception propagates (S3 read failure aborts startup) — the migration does
# not silently swallow S3 read errors. Must run AFTER the MQTT client is
# constructed so the publish step has something to publish through.
migrate_on_startup(
    s3_getter=s3.load_latest_config,
    sqlite_writer=sqlite.put_config,
    mqtt_publisher=lambda cfg_dict: _mqtt_client_publish_config(cfg_dict),
    s3_writer=s3.save_config_snapshot,
)


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

    cfg, err = _build_sign_config_from_request(data)
    if err is not None or cfg is None:
        return err if err is not None else (jsonify({"error": "invalid config"}), 400)
    _save_and_publish(cfg)
    return jsonify({"status": "ok"})


# Sign upgrade coordination (Pi loader and app's check-for-update
# handler both query this on every boot / every MQTT hint)


def _resolve_boot_config() -> BootConfig:
    """Compute the boot config Flask serves via `/api/sign/boot-config`.

    Delegates to `lib_shared.boot_config.from_heroku_or_git` so the
    Flask server, the loader, and the app-side `check_for_update`
    handler all use the same logic (HEROKU_SLUG_COMMIT preferred,
    git rev-parse HEAD fallback). Returns an empty-sha BootConfig
    if both fail — the endpoint translates that into a 500.
    """
    repo_root = Path(__file__).resolve().parent.parent
    return _boot_config_from_heroku_or_git(repo_root)


@app.route("/api/sign/boot-config", methods=["GET"])
@api_login_required
def api_sign_boot_config():
    """GET /api/sign/boot-config — return the commit SHA the Pi should run.

    The Pi queries this on every boot (via the loader) AND on every
    `check-for-update` MQTT hint (via the app's registered handler).
    Returns `{"expected_sha": "<sha>"}` where `<sha>` is
    `HEROKU_SLUG_COMMIT` (production) or local `git rev-parse HEAD`
    (local dev). Authenticated via the existing `X-API-Key` header —
    the same key the device uses for /api/config and /api/messages.

    Returns 500 only when both lookups fail (no env var and git
    invocation failed); 401 when the API key is missing or wrong.

    Path is defined as a constant in `lib_shared.boot_config`
    (`BOOT_CONFIG_PATH`) so the loader and the app both import
    the same string — typos or renames are caught at import time.
    """
    config = _resolve_boot_config()
    if not config.expected_sha:
        return jsonify({"error": "could not resolve expected SHA"}), 500
    return jsonify({
        "expected_sha": config.expected_sha,
        "short_sha": config.short_sha,
    })


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
    cfg_dict = cfg.to_dict()
    sqlite.put_config(cfg)
    try:
        s3.save_config_snapshot(cfg_dict)
    except Exception as e:
        logger.warning("Config S3 snapshot failed: %s", e)
    logger.info(
        "[flask] _save_and_publish: publishing config envelope "
        "rotation=%s text=(speed=%d, color=#%06x) pacing=(fade=%s, hold=%s)",
        [(e["name"], e["enabled"]) for e in cfg_dict["effects_settings"]["effects"]],
        cfg_dict["text_settings"]["speed"],
        cfg_dict["text_settings"]["color"],
        cfg_dict["effects_settings"]["fade_seconds"],
        cfg_dict["effects_settings"]["hold_seconds"],
    )
    _mqtt_client_publish_config(cfg_dict)


# Canonical set of effect class names the device knows about. Mirrors
# `lib_shared.models._DEFAULT_EFFECTS_LIST_FULL` (the device-side
# `_EFFECT_CLASSES` map in heart-matrix-controller/main.py is keyed on the
# same names). Used by `_build_sign_config_from_request` to reject
# incoming entries whose name isn't in this set.
_KNOWN_EFFECT_NAMES = frozenset(
    [
        "Hyperspace",
        "VideoDisplay",
        "PngDisplay",
        "Honeycomb",
        "Flame",
        "Fireworks",
        "NightSky",
    ]
)


def _build_sign_config_from_request(data: dict) -> tuple:
    """Validate an incoming config payload and build a SignConfig from it.

    Runs the migration registry at the top so v1 inputs are normalized to
    v2 before validation. Validates the new fields (effects entries,
    behavior fields, recent_count, text fields) and returns per-field error
    messages.

    Args:
        data: dict — the parsed JSON request body.

    Returns:
        A `(SignConfig | None, Response | None)` tuple. On success, the
        first element is the constructed SignConfig and the second is None.
        On failure, the first element is None and the second is a Flask
        JSON response with HTTP 400 and `{"error": "<message>"}`.
    """
    if not isinstance(data, dict):
        return None, (jsonify({"error": "expected JSON object"}), 400)

    # Normalize to current version before validation so v1 payloads are
    # accepted through the same code path as v2.
    data = migrate(data, current_version=SignConfig.CURRENT_VERSION)

    # Validate effects_settings.
    es = data.get("effects_settings")
    if es is not None:
        if not isinstance(es, dict):
            return None, (jsonify({"error": "effects_settings must be an object"}), 400)
        effects_list = es.get("effects")
        if not isinstance(effects_list, list):
            return None, (jsonify({"error": "effects_settings.effects must be a list"}), 400)
        for idx, entry in enumerate(effects_list):
            if not isinstance(entry, dict):
                return None, (
                    jsonify({"error": f"effects_settings.effects[{idx}]: must be an object"}),
                    400,
                )
            name = entry.get("name")
            enabled = entry.get("enabled")
            if not isinstance(name, str):
                return None, (
                    jsonify({"error": (f"effects_settings.effects[{idx}]: missing or invalid 'name'")}),
                    400,
                )
            if not isinstance(enabled, bool):
                return None, (
                    jsonify({"error": (f"effects_settings.effects[{idx}]: missing or invalid 'enabled'")}),
                    400,
                )
            if name not in _KNOWN_EFFECT_NAMES:
                return None, (
                    jsonify({"error": f"effects_settings.effects: unknown effect '{name}'"}),
                    400,
                )
        for field in ("fade_seconds", "hold_seconds", "intro_seconds", "idle_seconds"):
            v = es.get(field)
            if v is not None and (not isinstance(v, (int, float)) or v < 0):
                return None, (
                    jsonify({"error": (f"effects_settings.{field}: must be a non-negative number")}),
                    400,
                )
        rc = es.get("recent_count")
        if rc is not None:
            if isinstance(rc, bool) or not isinstance(rc, int) or rc < 1:
                return None, (
                    jsonify({"error": ("effects_settings.recent_count: must be a positive integer")}),
                    400,
                )

    # Validate text_settings.
    ts = data.get("text_settings")
    if ts is not None:
        if not isinstance(ts, dict):
            return None, (jsonify({"error": "text_settings must be an object"}), 400)
        speed = ts.get("speed")
        if speed is not None:
            if isinstance(speed, bool) or not isinstance(speed, int) or not (1 <= speed <= 5):
                return None, (
                    jsonify({"error": "text_settings.speed: must be an integer in 1..5"}),
                    400,
                )
        color = ts.get("color")
        if color is not None:
            if isinstance(color, bool) or not isinstance(color, int) or not (0 <= color <= 0xFFFFFF):
                return None, (
                    jsonify({"error": ("text_settings.color: must be an integer in 0..0xFFFFFF")}),
                    400,
                )
        te = ts.get("text_effect")
        if te is not None and te not in ("scroll",):
            return None, (
                jsonify({"error": (f"text_settings.text_effect: must be one of ('scroll',), got {te!r}")}),
                400,
            )

    # All checks passed; construct the SignConfig (from_dict runs migrate()
    # again as defense-in-depth, which is a no-op for an already-migrated dict).
    cfg = SignConfig.from_dict(data)
    return cfg, None


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

        # Text settings: speed (1..5), color, text effect.
        ts_form = cfg.text_settings
        speed_raw = request.form.get("text_settings_speed")
        if speed_raw is not None and speed_raw != "":
            try:
                speed_val = int(speed_raw)
                if 1 <= speed_val <= 5:
                    ts_form.speed = speed_val
            except ValueError:
                pass
        color = request.form.get("text_settings_color")
        if color is not None and color != "":
            try:
                ts_form.color = int(color, 16) & 0xFFFFFF
            except ValueError:
                pass
        te = request.form.get("text_settings_text_effect")
        if te:
            ts_form.text_effect = te
        cfg.text_settings = ts_form

        # Effect settings: pacing (fade/hold/intro/idle seconds), recent_count,
        # and the rotation list (handled by the multi-effect form below).
        es_form = cfg.effects_settings
        for field in ("fade_seconds", "hold_seconds", "intro_seconds", "idle_seconds"):
            raw = request.form.get(f"effects_settings{field}")
            if raw is not None and raw != "":
                try:
                    setattr(es_form, field, float(raw))
                except ValueError:
                    pass
        rc_raw = request.form.get("effects_settings")
        if rc_raw is not None and rc_raw != "":
            try:
                es_form.recent_count = int(rc_raw)
            except ValueError:
                pass

        # Effect rotation list: the form posts the canonical order with each
        # entry's `enabled` checkbox value (or absence). Rebuild the list
        # preserving only known names.
        enabled_map = {}
        for name in request.form.getlist("effect_name"):
            enabled_map[name] = True
        # Any canonical name absent from the form list is treated as disabled
        # (its checkbox wasn't ticked). We rebuild the list in the canonical
        # order from the current model defaults so ordering is preserved.
        new_effects = []
        for entry in _DEFAULT_EFFECTS_LIST_FULL:
            new_effects.append({"name": entry["name"], "enabled": entry["name"] in enabled_map})
        es_form.effects = new_effects
        cfg.effects_settings = es_form

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
        speed_labels=ScrollerBase.SPEED_LABELS,
        # Deployed SHA: what Flask expects the Pi to be running.
        # This is the last-deployed commit, not the Pi's literal live
        # running SHA (those differ during the ~12s swap window). See
        # ISA.md ISC-A3 — querying the Pi's live SHA is out of scope.
        deployed_sha_short=_resolve_boot_config().short_sha or None,
        deployed_sha_full=_resolve_boot_config().expected_sha or None,
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
# `MQTT_HOST` + `MQTT_WS_PORT` — `ws://localhost:9001` for native
# Mosquitto, `wss://io.adafruit.com` for Adafruit IO). Without this,
# the browser console fills with "Connecting to 'ws://<host>:<port>/mqtt'
# violates the following Content Security Policy directive" errors and
# the preview page never receives live envelopes.
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
    """Derive the MQTT-over-WebSocket URL from MQTT_HOST + MQTT_WS_PORT.

    Resolution order:
      1. `MQTT_WS_URL` (full URL) — wins outright if set.
      2. `MQTT_WS_PORT` (port only) — combined with `MQTT_HOST`.
      3. Default port: 9001 for loopback (`127.0.0.1`), 443 for
         everything else (broker default). Scheme: `ws://` for
         loopback, `wss://` otherwise.

    9001 is the protocol default for Mosquitto. The `scripts/start-app.sh`
    Docker flow maps host 9002 → container 9001 (to avoid MinIO's
    console on host 9001); if you use that flow, set
    `MQTT_WS_PORT = "9002"`. For a remote broker on a non-standard
    port, set `MQTT_WS_PORT` to its WS port.

    The literal `localhost` is rewritten to `127.0.0.1` in the final
    URL, regardless of whether it came from `MQTT_HOST` or from
    `MQTT_WS_URL`. The TCP connection lands at the same loopback
    either way, but Chromium-based browsers with built-in tracker
    blockers (Arc, in particular) have been observed to block
    `ws://localhost:9001/mqtt` while letting `ws://127.0.0.1:9001/mqtt`
    through — the blocker applies heuristics to `localhost` URLs that
    it doesn't apply to literal IPs. Forcing the IP sidesteps the
    false positive. Any other host (a real DNS name, a non-loopback
    IP, IPv6) passes through unchanged.
    """
    explicit = _cfg.if_exists("MQTT_WS_URL")
    if explicit:
        # See the Arc note in the docstring above.
        derived = explicit.replace("localhost", "127.0.0.1")
        print(f"[DEBUG] _derive_mqtt_ws_url: explicit={explicit!r} -> {derived!r}")
        return derived
    host = _cfg.if_exists("MQTT_HOST") or "127.0.0.1"
    if host == "localhost":
        host = "127.0.0.1"
    explicit_port = _cfg.if_exists("MQTT_WS_PORT")
    if explicit_port:
        port = str(explicit_port)
    elif host == "127.0.0.1":
        port = "9001"
    else:
        port = "443"
    scheme = "ws" if host == "127.0.0.1" else "wss"
    if scheme == "ws":
        derived = f"{scheme}://{host}:{port}/mqtt"
    elif port != "443":
        derived = f"{scheme}://{host}:{port}/mqtt"
    else:
        derived = f"{scheme}://{host}/mqtt"
    print(f"[DEBUG] _derive_mqtt_ws_url: host={host!r} port={port!r} -> {derived!r}")
    return derived


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
            # The browser subscribes to the exact same wire-format topic
            # the Flask process publishes on. Paho is a thin wrapper —
            # it does no broker-specific translation — so the operator
            # is responsible for setting MQTT_TOPIC to the full path
            # their broker expects (e.g. for Adafruit IO that's
            # "{username}/feeds/{feedname}"). The paho subscriber, the
            # Flask publisher, and the browser all share this single
            # source of truth, so they always agree.
            "MQTT_TOPIC": _cfg.if_exists("MQTT_TOPIC") or "",
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
