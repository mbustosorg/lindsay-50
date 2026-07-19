"""Flask app for heart-message-manager.

Handles Twilio webhooks, stores messages to SQLite (with S3 backup),
publishes to Adafruit IO, and serves the admin UI.
"""

from __future__ import annotations

import functools
import html
import json
import logging
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
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
# `MQTT_STATUS_TOPIC` is intentionally NOT in REQUIRED_KEYS: it has a
# derived default of `f"{MQTT_TOPIC}-status"` (see `_resolve_status_topic`
# below) and operators shouldn't have to set an empty string just to get
# the default. Set it explicitly only if you want a different topic name.
_cfg = get_config(REQUIRED_KEYS)

import sqlite, s3
from server_time import format_from_iso, now_utc_iso
from lib_shared.boot_config import BootConfig, from_heroku_or_git as _boot_config_from_heroku_or_git
from lib_shared.config_migrations import migrate, migrate_on_startup
from lib_shared.effects_loader import load_effects_settings
from lib_shared.models import SignConfig, FilterRule, Message
from lib_shared.models import EffectsSettings
from lib_shared.models import MessageEnvelope
from lib_shared.scroller_base import ScrollerBase
from lib_shared.sign_status import LatestSignStatus, REQUIRED_SNAPSHOT_KEYS

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
# (no Flask-side subscriber on the envelope topic). The device and the
# browser both subscribe to the envelope topic on their own. The status
# topic IS subscribed by Flask (Decision 4 in openspec/changes/
# add-sign-status-reports/design.md): the latest snapshot is held in
# memory for the browser's load-time hydration GET /api/sign-status
# call.
from lib_shared.paho_mqtt_client import PahoMqttClient


def _noop_dispatch(_payload: str) -> None:
    """Flask no longer subscribes to MQTT envelopes; drop them on the floor."""


def _on_status_payload(raw_payload: str) -> None:
    """Decode a status-topic payload and store it in `latest_status`.

    Wired as `status_dispatch_callback` on the PahoMqttClient. Both
    arguments MUST be optional in the dispatch table — a malformed
    payload or a payload missing required keys is logged at WARN
    and dropped (the store is not replaced). Required-keys list lives
    in `lib_shared.sign_status.REQUIRED_SNAPSHOT_KEYS` (one source
    of truth for the snapshot schema).
    """
    try:
        parsed = json.loads(raw_payload)
    except (ValueError, TypeError) as exc:
        logger.warning(
            "[flask] _on_status_payload: parse failed (topic=%s): %s",
            _mqtt_status_topic,
            exc,
        )
        return
    if not isinstance(parsed, dict):
        logger.warning(
            "[flask] _on_status_payload: payload is not a dict: %r",
            parsed,
        )
        return
    missing = [k for k in REQUIRED_SNAPSHOT_KEYS if k not in parsed]
    if missing:
        logger.warning(
            "[flask] _on_status_payload: missing required keys %s; dropping payload",
            ", ".join(missing),
        )
        return
    try:
        latest_status.update(parsed)
    except ValueError as exc:
        logger.warning(
            "[flask] _on_status_payload: store rejected payload: %s",
            exc,
        )
        return
    logger.debug(
        "[flask] _on_status_payload: stored snapshot updated_at=%s",
        parsed.get("updated_at"),
    )


# Latest-snapshot in-memory store. Populated by the status_dispatch_callback
# below; read by GET /api/sign-status. Flask owns one instance for the
# whole process lifetime (Decision 4 in design.md). The store is reset
# to empty on every Flask restart — see "Flask restart loses the latest
# snapshot" in the Risks section.
latest_status = LatestSignStatus()


# Resolve the status topic. Default rule (Decision 2): "{MQTT_TOPIC}-status"
# — operators can override via `MQTT_STATUS_TOPIC` in settings.toml or env
# (env wins). Adafruit IO requires an explicit feed creation; this matches
# the device's convention in heart-matrix-controller/main.py.
def _resolve_status_topic() -> str:
    raw = _cfg.if_exists("MQTT_STATUS_TOPIC") or ""
    if raw.strip():
        return raw
    base = _cfg.if_exists("MQTT_TOPIC") or ""
    return f"{base}-status"


_mqtt_status_topic = _resolve_status_topic()
_raw_mqtt_status_topic = _cfg.if_exists("MQTT_STATUS_TOPIC")
logger.info(
    "[flask] status topic resolved to %r (source: %s)",
    _mqtt_status_topic,
    "explicit MQTT_STATUS_TOPIC" if (_raw_mqtt_status_topic or "").strip() else "derived from MQTT_TOPIC",
)


_mqtt_client = PahoMqttClient(
    dispatch_callback=_noop_dispatch,
    host=_cfg.MQTT_HOST,
    port=_cfg.MQTT_PORT,
    username=_cfg.MQTT_USERNAME,
    password=_cfg.MQTT_PASSWORD,
    topic=_cfg.MQTT_TOPIC,
    status_topic=_mqtt_status_topic,
    status_dispatch_callback=_on_status_payload,
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
        # Pretty-print every incoming field so the operator can see exactly
        # what Twilio sent. Twilio's MMS webhooks include NumMedia,
        # MediaUrl0..N, MediaContentType0..N, SmsSid, MessageStatus,
        # ApiVersion, etc. — we want all of them visible in journalctl
        # for debugging, not just From/Body. `default=str` coerces any
        # non-JSON-serializable value (FileStorage, etc.) to its repr.
        import json as _json

        logger.info(
            "Twilio webhook: reconstructed_url=%s X-Forwarded-Proto=%s " "X-Twilio-Signature=%s",
            webhook_url,
            forwarded_proto,
            (signature[:12] + "...") if signature else "(none)",
        )
        logger.info(
            "Twilio webhook fields (%d):\n%s",
            len(params),
            _json.dumps(params, indent=2, sort_keys=True, default=str),
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


def _process_inbound_message(req) -> Response:
    """Shared message processing for both real Twilio webhooks and test injections.

    Sync phase (the request handler): parse Twilio's form fields, build the
    TwiML response, dispatch a background thread if NumMedia > 0, return.
    Twilio's webhook response budget is 15 s — D13 mandates we never block
    the request on media downloads/uploads.

    Async phase (`_process_inbound_media_async`): for MMS payloads, a daemon
    ThreadPoolExecutor downloads attachments via Twilio Basic Auth, copies
    them to S3, persists the Message (text + completed media list), and
    publishes the MessageEnvelope over MQTT — all on a non-daemon future
    handle, so the request returns 200/TwiML before any of this work happens.

    The MessageSid-keyed dedupe guard prevents double-processing when
    Twilio retries a webhook while the background thread is still in
    flight. Without it, a SLO-flaky Twilio path could result in two
    `MessageEnvelope` publishes for the same SID.

    Empty body + no media still returns 204 (no-op today, preserved); empty
    body + media is accepted (background thread publishes a `body=""` +
    populated media list — the existing `scroller.set_text("", ...)` path
    in `EffectsCoordinator.out → in` handles the blank text and routes
    the cycler into `background` mode after fade-in).
    """
    sender = req.form.get("From", "")
    body = req.form.get("Body", "").strip()
    # `NumMedia` is optional on SMS-only webhooks. Twilio sends "0" on a
    # pure SMS; absent entirely on the test-injection path. Treat absent
    # as "no media" so existing tests don't have to set it.
    try:
        num_media = int(req.form.get("NumMedia", "0") or "0")
    except ValueError:
        num_media = 0
    message_sid = req.form.get("MessageSid", "") or ""

    # Collect all attachment (type, url) pairs up front. Twilio sends them
    # as indexed form fields MediaContentType0..N / MediaUrl0..N. We iterate
    # by index — `num_media` is the upper bound, missing entries stop the
    # walk. The async thread reads them from a thread-safe copy of
    # `req.form` (Werkzeug's `MultiDict` is not strictly thread-safe).
    media_pairs = []
    for i in range(num_media):
        ctype = req.form.get(f"MediaContentType{i}", "")
        url = req.form.get(f"MediaUrl{i}", "")
        if not ctype or not url:
            continue
        media_pairs.append((ctype, url))

    logger.info(
        "From=%r Body=%r NumMedia=%d sid=%s",
        sender,
        body,
        num_media,
        message_sid[:12] + "..." if message_sid else "(none)",
    )

    # Empty body + no media: short-circuit with 204 (no background work,
    # no S3, no MQTT). The same behavior the sync path produced before
    # the MMS work — preserved.
    if not body and not media_pairs:
        return Response("", status=204)

    # 204 short-circuited above; from here on we always respond 200.
    cfg = sqlite.get_config()
    sign_name = cfg.sign.name if cfg.sign else "Lindsay's Heart"
    reply_body = body if body else "(no message)"
    reply = f"{sign_name} got your message: {html.escape(reply_body)}"
    twiml = Response(
        f"<Response><Message>{reply}</Message></Response>",
        status=200,
        mimetype="text/xml",
    )

    # No media: synchronous path (preserves today's behavior). Publishes
    # the MessageEnvelope immediately, no background thread.
    if not media_pairs:
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
        try:
            sqlite.put_message(msg)
            assert _mqtt_client is not None
            _mqtt_client.publish_envelope(MessageEnvelope("message", msg.to_dict()))
        except Exception as e:
            logger.error("Post-webhook processing failed: %s", e)
        return twiml

    # MMS path: respond 200/TwiML now; background thread does the rest.
    # Dedupe guard: if Twilio retries the same SID while a previous worker
    # is still running, return 200/TwiML without spawning a duplicate.
    if message_sid:
        with _INBOUND_DEDUPE_LOCK:
            in_flight = _INBOUND_DEDUPE.get(message_sid)
            if in_flight is not None and not in_flight.done():
                logger.info(
                    "Twilio webhook dedupe: sid=%s already in flight; returning 200 immediately",
                    message_sid,
                )
                return twiml
            slot = _MediaFuture()
            # Carry the SID inside the slot so the async worker can pop
            # the dedupe table in its `finally` block — the table is keyed
            # by SID, not by slot identity, so without this back-reference
            # the worker can only mark the slot `done()` and the table
            # entry would leak until the next MMS for the same SID.
            slot._sid = message_sid
            _INBOUND_DEDUPE[message_sid] = slot
    else:
        # Test-injection path / non-MMS webhook — no dedupe tracking.
        slot = _MediaFuture()

    # Build the ThreadPoolExecutor lazily on the first MMS we see so the
    # SMS-only path doesn't pay the cost of a worker pool. `max_workers=4`
    # matches the per-MMS attachment cap in `_process_inbound_media_async`
    # (the inner pool pools per attachment, not per webhook).
    global _MMS_EXECUTOR
    if _MMS_EXECUTOR is None:
        _MMS_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="mms")

    worker_payload = {
        "sender": sender,
        "body": body,
        "media_pairs": media_pairs,
        "slot": slot,
    }
    _MMS_EXECUTOR.submit(_process_inbound_media_async, worker_payload)
    return twiml


class _MediaFuture:
    """A stand-in for `concurrent.futures.Future` used as a dedupe slot.

    We need a thread-safe handle to track in-flight inbound processing per
    MessageSid. The real `Future` from `concurrent.futures` works fine, but
    we don't have one yet at the dedupe-check site (the worker is about to
    be submitted, not yet executing). A small class with `done()` / `set_done()`
    covers the exact surface the dedupe path needs and keeps the dependency
    surface flat — no `_as_completed` / `wait` / `cancel` surface area we
    don't use. The async thread calls `set_done()` in its finally block.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._done = False
        # Back-reference to the Twilio MessageSid this slot was registered
        # under. Set by the dedupe-registration site (line ~437); the async
        # worker reads it in its `finally` block to remove the entry from
        # `_INBOUND_DEDUPE` (keyed by SID, not by slot identity). Declared
        # here so Pylance / mypy see it as a real attribute and not a
        # dynamic assignment to an unknown member.
        self._sid: str | None = None

    def done(self) -> bool:
        with self._lock:
            return self._done

    def set_done(self) -> None:
        with self._lock:
            self._done = True


# Dedupe-guard table — keyed by Twilio `MessageSid`. Holds an in-flight
# future (or a stand-in) per SID; entries are popped when the worker
# finishes (success OR exception; see the `finally` block in
# `_process_inbound_media_async`). Kept in-process only — a Flask restart
# wipes the guard, which is fine because Twilio's retry budget (≤3) is
# shorter than typical restart time.
_INBOUND_DEDUPE: dict[str, _MediaFuture] = {}
_INBOUND_DEDUPE_LOCK = threading.Lock()
_MMS_EXECUTOR: ThreadPoolExecutor | None = None


def _process_inbound_media_async(payload: dict) -> None:
    """Background-thread body for MMS webhooks (design D13).

    1. Download every `MediaUrl{i}` via Twilio Basic Auth (`s3.log_media`).
       Runs the downloads in parallel through a ThreadPoolExecutor — `s3.log_media`
       is HTTP-bound, parallelism matters more than thread count for typical
       1-3 attachment MMS.
    2. Build the `media: list[{type, url}]` list, dropping items where the
       download or S3 put failed (WARNING logged inside `log_media`).
    3. Persist the `Message` (text + completed media list) to S3 + SQLite.
       `s3.log_message` stays exactly as-is — we add a `media` field to
       its JSON payload and `Message.from_dict` defaults any missing field
       to `[]` on the consumer side, so legacy S3 message JSON files
       remain valid.
    4. Publish `MessageEnvelope` over MQTT exactly once.
    5. Release the dedupe guard in the `finally` block so a worker crash
       doesn't leave the SID locked.

    On any unhandled exception we log CRITICAL and let the `finally` clean
    the dedupe guard so subsequent retries for the same SID don't deadlock.
    """
    sender = payload["sender"]
    body = payload["body"]
    media_pairs = payload["media_pairs"]
    slot = payload["slot"]
    sid = ""
    try:
        # Twilio's webhook always carries MessageSid; the test-injection
        # path may not. We don't need the SID inside the worker (the
        # dedupe table already references this slot) but logging it makes
        # journalctl entries easy to correlate. Pull it from the request
        # form is not safe from this thread (form is not thread-safe);
        # however, MMS webhooks always include `MessageSid` so we look
        # at `payload` rather than re-read the form here.
        # `payload["slot"]` is our dedupe slot; the SID is intentionally
        # not threaded through — see the design D13 dedupe contract.
        logger.info(
            "MMS async: starting downloads for %d attachment(s) sender=%r",
            len(media_pairs),
            sender,
        )
        media_list, download_failures = _download_media_in_parallel(media_pairs)
        if download_failures:
            logger.warning(
                "MMS async: %d of %d attachment(s) failed (continuing with survivors)",
                download_failures,
                len(media_pairs),
            )
        # One-line summary of what survived the download+upload
        # round-trip. The browser preview's `BrowserMediaOverlay` will
        # render an empty list when `media_list` is empty, so this
        # log line is the single best signal for "why isn't the
        # image showing up in the preview?". Grep the Flask log
        # for `MMS async: built media_list` after each test post.
        logger.info(
            "MMS async: built media_list items=%d content_types=%s keys=%s",
            len(media_list),
            [m.get("type") for m in media_list],
            [m.get("url") for m in media_list],
        )

        msg = Message(
            id=str(uuid.uuid4()),
            sender=sender,
            body=body,
            received_at=now_utc_iso(),
            media=media_list,
        )

        try:
            s3.log_message(msg)
        except Exception as e:
            logger.warning("S3 logging failed (will continue): %s", e)

        try:
            sqlite.put_message(msg)
            assert _mqtt_client is not None
            _mqtt_client.publish_envelope(MessageEnvelope("message", msg.to_dict()))
        except Exception as e:
            logger.error("Post-MMS-webhook processing failed: %s", e)
    except Exception as e:
        logger.exception("MMS async: unhandled exception (sid will be released): %s", e)
    finally:
        # Always release the dedupe slot — even on uncaught exceptions —
        # so a future retry of the same SID doesn't deadlock.
        try:
            with _INBOUND_DEDUPE_LOCK:
                slot.set_done()
                # Pop the slot for our SID if it's still us. `_INBOUND_DEDUPE`
                # is keyed by SID; we re-read the SID from the request form
                # only when needed. In practice MMS webhooks always set
                # MessageSid so we keep a back-reference in the slot.
                sid_attr = getattr(slot, "_sid", "")
                if sid_attr and _INBOUND_DEDUPE.get(sid_attr) is slot:
                    del _INBOUND_DEDUPE[sid_attr]
        except Exception as e:  # noqa: BLE001 — last-ditch log + drop
            logger.warning("MMS async: failed to release dedupe slot: %s", e)


def _download_media_in_parallel(media_pairs: list[tuple[str, str]]) -> tuple[list[dict], int]:
    """Download + upload each (content_type, url) pair in parallel.

    Returns:
        A `(media_list, failures)` tuple:
          - `media_list` is the on-wire list of `{"type", "url"}` dicts
            in the same order as `media_pairs` (so failed items leave
            holes / are dropped — see the decision below).
          - `failures` is the count of items where the download or
            upload returned None. The caller logs a single WARNING.

    Implementation note: `s3.log_media` is HTTP-Bound (Twilio's Basic-Auth
    download) and CPU-very-light (a single boto3 put). A small
    ThreadPoolExecutor with `max_workers=min(len(pairs), 4)` keeps the
    webhook fan-out bounded — a Twilio MMS with 6 attachments still only
    spawns 4 concurrent downloads.
    """
    if not media_pairs:
        return [], 0

    max_workers = min(len(media_pairs), 4)
    media_list: list[dict] = []
    failures = 0

    # Reserve index slots so we can iterate in input order. The wire format
    # preserves order — a panel-cycling client might rely on "first
    # attachment shows first". Failed slots become `None` and are
    # filtered out before publish.
    results: list[dict | None] = [None] * len(media_pairs)

    def _download_one(idx_and_pair):
        idx, (ctype, url) = idx_and_pair
        key = s3.log_media(ctype, url)
        return idx, ctype, key

    with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="mms-dl") as pool:
        futures = [pool.submit(_download_one, (i, pair)) for i, pair in enumerate(media_pairs)]
        for fut in futures:
            try:
                idx, ctype, key = fut.result()
            except Exception as exc:
                logger.warning("MMS async: download worker raised: %s", exc)
                failures += 1
                continue
            if key is None:
                failures += 1
                continue
            results[idx] = {"type": ctype, "url": key}

    media_list = [r for r in results if r is not None]
    return media_list, failures


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
    return jsonify(
        {
            "expected_sha": config.expected_sha,
            "short_sha": config.short_sha,
        }
    )


@app.route("/api/sign-status", methods=["GET"])
def api_sign_status():
    """GET /api/sign-status — return the most recent snapshot Flask has.

    Decision 4 + Decision 7 in openspec/changes/add-sign-status-reports/
    design.md: the browser does a single load-time fetch against this
    endpoint for hydration; the browser does NOT call this on a timer.
    Always returns HTTP 200 — even when the in-memory store is empty
    (browser sees `snapshot: null`). Does NOT compute or include a
    `state` field — state is browser-side (the Flask store has no
    notion of "live / unknown / offline"; it just holds the latest
    payload).
    """
    # Module-level `latest_status` was instantiated near the top of
    # this file — it's a `LatestSignStatus` instance, with a lock-
    # guarded snapshot() and received_at_wallclock() methods.
    return jsonify(
        {
            "snapshot": latest_status.snapshot(),
            "received_at": latest_status.received_at_wallclock(),
        }
    )


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


@app.route("/api/media/<path:s3_key>")
@api_login_required
def api_media(s3_key):
    """Return 302 to a freshly-signed S3 URL for the given media key (design D3).

    Auth: same ``api_login_required`` as other API routes — accepts the
    device's X-API-Key header OR a logged-in browser session. The Pi's
    image-display effect and the browser preview both follow the redirect
    the same way.

    Bytes NEVER flow through Flask: the 302's ``Location`` header points
    to ``boto3.generate_presigned_url(..., ExpiresIn=3600)``, which the
    client GETs directly from S3. Flask stays on the auth boundary (it
    gates who can mint signed URLs) without paying Heroku egress twice.

    Error map (spec):
      - missing / wrong API key → 401 (decorator)
      - ``..`` in path  → 400 (path-traversal guard, evaluated BEFORE the
        S3 call so a malicious key never tries to reach S3)
      - S3 raises on signing (e.g. ``NoSuchKey`` when the key doesn't
        exist) → 404 with a JSON ``{"error": "..."}`` body
      - S3 raises on a transient outage (network, IAM, throttle) → 502
        with a JSON ``{"error": "..."}`` body

    Diagnostic logging (issue #26 follow-up): every request is logged
    with the S3 key, the requester (Pi vs browser, distinguished via
    the ``X-API-Key`` header vs the Flask session cookie), the outcome
    (302 redirect, 400 invalid path, 404 signing failed, 502 transient),
    and the resolved signed-URL host (when signing succeeds). The
    User-Agent is included so we can tell the browser preview's
    `<img>` / `<video>` fetch from the Pi's MediaCycler fetch from
    curl-style debugging hits. This is the ground truth for "did the
    image actually get requested" — when the browser reports
    fade-in/out but no network call appears in DevTools, this line
    tells us whether the proxy was hit at all.
    """
    requester = "pi" if request.headers.get("X-API-Key") else "browser"
    user_agent = request.headers.get("User-Agent", "")[:80]
    if ".." in s3_key or s3_key.startswith("/") or "//" in s3_key:
        logger.warning(
            "/api/media rejected invalid key=%s requester=%s ua=%r",
            s3_key,
            requester,
            user_agent,
        )
        return jsonify({"error": "invalid S3 key"}), 400
    try:
        signed = s3.signed_media_url(s3_key)
    except Exception as exc:  # boto3 raises BotoCoreError / ClientError
        logger.warning(
            "/api/media signing failed key=%s requester=%s ua=%r err=%s",
            s3_key,
            requester,
            user_agent,
            exc,
        )
        return jsonify({"error": "media not found"}), 404
    if not signed:
        logger.warning(
            "/api/media empty signed url key=%s requester=%s ua=%r",
            s3_key,
            requester,
            user_agent,
        )
        return jsonify({"error": "media not found"}), 404
    # Extract the S3 host (the `netloc` of the signed URL) so the log
    # shows the actual destination the client will GET. The full
    # signed URL is intentionally NOT logged — it carries a query
    # string with credentials and is short-lived.
    from urllib.parse import urlparse

    signed_host = urlparse(signed).netloc
    logger.info(
        "/api/media 302 key=%s requester=%s ua=%r signed_host=%s",
        s3_key,
        requester,
        user_agent,
        signed_host,
    )
    response = Response("", status=302)
    response.headers["Location"] = signed
    return response


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
# the loader-driven `lib_shared/config/effects_settings.json` (the
# device-side `_EFFECT_CLASSES` map in heart-matrix-controller/main.py is
# keyed on the same names). Used by `_build_sign_config_from_request` to
# reject incoming entries whose name isn't in this set. The frozenset is
# derived from `load_effects_settings()["effects"]` at startup so an
# operator's `config_overrides/effects_settings.json` is honored without
# a code change.
_KNOWN_EFFECT_NAMES = frozenset(entry["name"] for entry in load_effects_settings().get("effects", []))
logger.info(
    "[flask] _KNOWN_EFFECT_NAMES derived from loader: count=%d names=%s",
    len(_KNOWN_EFFECT_NAMES),
    sorted(_KNOWN_EFFECT_NAMES),
)


def _build_sign_config_from_request(data: dict) -> tuple:
    """Validate an incoming config payload and build a SignConfig from it.

    Runs the migration registry at the top so v1 inputs are normalized to
    v2 before validation. Validates the new fields (effects entries,
    behavior fields, lookback_days, selector_algorithm, text fields)
    and returns per-field error messages.

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
        # `lookback_days` bounds mirror `EffectsSettings.MIN/MAX_LOOKBACK_DAYS`.
        # The validator here is a defense-in-depth duplicate — `from_dict`
        # also validates, but this returns a structured 400 with the exact
        # field name so the wire path doesn't need to import the model
        # class just to discover the bounds.
        lb = es.get("lookback_days")
        if lb is not None:
            min_lb = EffectsSettings.MIN_LOOKBACK_DAYS
            max_lb = EffectsSettings.MAX_LOOKBACK_DAYS
            if isinstance(lb, bool) or not isinstance(lb, int) or not (min_lb <= lb <= max_lb):
                return None, (
                    jsonify(
                        {"error": (f"effects_settings.lookback_days: must be an integer " f"in {min_lb}..{max_lb}")}
                    ),
                    400,
                )
        alg = es.get("selector_algorithm")
        if alg is not None and alg not in EffectsSettings.VALID_SELECTOR_ALGORITHMS:
            return None, (
                jsonify(
                    {
                        "error": (
                            f"effects_settings.selector_algorithm: must be one of "
                            f"{EffectsSettings.VALID_SELECTOR_ALGORITHMS}, got {alg!r}"
                        )
                    }
                ),
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

        # Pretty-print the raw POST so an operator (or the next debugging
        # session) can see EXACTLY what the /settings form submitted.
        # Without this, form field-name mismatches look like "the value
        # silently didn't save" with no on-the-wire evidence. We log at
        # INFO because the volume is low (1 POST per settings save) and
        # the diagnostic value is high.
        import json as _json

        form_keys = sorted(request.form.keys())
        logger.info(
            "[settings] POST /settings raw_form_keys=%d form=%s",
            len(form_keys),
            _json.dumps(
                {k: request.form.get(k) for k in form_keys},
                indent=2,
                sort_keys=True,
                default=str,
            ),
        )

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

        # Effect settings: pacing (fade/hold/intro/idle seconds),
        # `lookback_days`, `selector_algorithm`, and the rotation list
        # (handled by the multi-effect form below).
        #
        # BUGFIX (2026-07-07): the previous handler built field names as
        # `f"effects_settings{field}"` which produced `effects_settingsfade_seconds`
        # — the template (templates/settings.html) actually sends
        # `effects_settings_fade_seconds` with a separating underscore. The
        # mismatch made `request.form.get(...)` return None for every pacing
        # field and `recent_count`, so setattr() never ran and saves looked
        # like "the value silently reverted". The per-field log below now
        # reports the raw value actually submitted vs. what landed on the cfg.
        es_form = cfg.effects_settings
        pacing_summary: dict[str, str] = {}
        for field in ("fade_seconds", "hold_seconds", "intro_seconds", "idle_seconds"):
            raw = request.form.get(f"effects_settings_{field}")
            if raw is not None and raw != "":
                try:
                    new_val = float(raw)
                    setattr(es_form, field, new_val)
                    pacing_summary[field] = f"POST={raw!r} saved={new_val}"
                except ValueError:
                    pacing_summary[field] = f"POST={raw!r} DROPPED (not a float)"
            else:
                pacing_summary[field] = "absent"
        # `lookback_days` is bounded 1..365 in the EffectsSettings model and
        # in the validated wire path. The form-side guard mirrors that
        # bounds check so an out-of-range submission lands as a no-op
        # rather than corrupting the config (the field would round-trip
        # to a 400 on the `/api/config` path on the next refresh anyway,
        # but we keep the form parity for symmetry).
        lb_raw = request.form.get("effects_settings_lookback_days")
        if lb_raw is not None and lb_raw != "":
            try:
                lb_val = int(lb_raw)
                if EffectsSettings.MIN_LOOKBACK_DAYS <= lb_val <= EffectsSettings.MAX_LOOKBACK_DAYS:
                    es_form.lookback_days = lb_val
                    pacing_summary["lookback_days"] = f"POST={lb_raw!r} saved={lb_val}"
                else:
                    pacing_summary["lookback_days"] = (
                        f"POST={lb_raw!r} DROPPED (out of range "
                        f"{EffectsSettings.MIN_LOOKBACK_DAYS}..{EffectsSettings.MAX_LOOKBACK_DAYS})"
                    )
            except ValueError:
                pacing_summary["lookback_days"] = f"POST={lb_raw!r} DROPPED (not an int)"
        else:
            pacing_summary["lookback_days"] = "absent"
        # `selector_algorithm` is a closed enum (`weighted` | `random`);
        # unknown values are dropped so a stale form (e.g. from a
        # downgraded browser tab) doesn't clobber the saved setting.
        alg_raw = request.form.get("effects_settings_selector_algorithm")
        if alg_raw is not None and alg_raw != "":
            if alg_raw in EffectsSettings.VALID_SELECTOR_ALGORITHMS:
                es_form.selector_algorithm = alg_raw
                pacing_summary["selector_algorithm"] = f"POST={alg_raw!r} saved={alg_raw!r}"
            else:
                pacing_summary["selector_algorithm"] = (
                    f"POST={alg_raw!r} DROPPED (not in {EffectsSettings.VALID_SELECTOR_ALGORITHMS})"
                )
        else:
            pacing_summary["selector_algorithm"] = "absent"
        logger.info(
            "[settings] effect pacing merge: %s",
            _json.dumps(pacing_summary, sort_keys=True),
        )

        # Effect rotation list: the form posts the canonical order with each
        # entry's `enabled` checkbox value (or absence). Rebuild the list
        # preserving only known names.
        enabled_map = {}
        for name in request.form.getlist("effect_name"):
            enabled_map[name] = True
        # Any canonical name absent from the form list is treated as disabled
        # (its checkbox wasn't ticked). We rebuild the list in the canonical
        # order from the loader-driven defaults so ordering is preserved
        # and the source of truth matches the JSON the Pi sees.
        new_effects = []
        for entry in load_effects_settings().get("effects", []):
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
        effects_settings=load_effects_settings(),
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
    # media-src is set explicitly because `default-src 'self'`
    # would otherwise be the fallback for `<video src=…>` loads
    # (the browser doesn't allow the S3 origin without an
    # explicit media-src, even though img-src is allowed). The
    # S3 origin is spliced in by `_set_preview_csp` from the
    # same config the S3 client reads.
    "media-src 'self'; "
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
        # The Flask /api/media/<key> route 302-redirects to a freshly-
        # signed S3 URL (presigned via boto3 in s3.py). The browser
        # follows the redirect and loads the image bytes from the S3
        # origin directly, so `img-src` must allow it. The origin
        # depends on the S3 config: dev (MinIO) uses the explicit
        # `AWS_S3_ENDPOINT_URL`; prod (real AWS) uses the virtual-hosted
        # style `https://<bucket>.s3.<region>.amazonaws.com`. We
        # compute it from the same config the S3 client uses
        # (`s3._s3_client`), so the CSP never disagrees with the
        # actual signed-URL origin.
        s3_origin = _derive_s3_origin()
        csp = _PREVIEW_CSP_BASE
        if ws_origin:
            csp = csp.replace(
                "connect-src 'self'",
                f"connect-src 'self' {ws_origin}",
            )
        if s3_origin:
            csp = csp.replace(
                "img-src 'self' data:",
                f"img-src 'self' data: {s3_origin}",
            )
            # media-src is the CSP directive for <video> and <audio>;
            # the S3 redirect target is the same origin as the image
            # load, so the same allow-list entry applies. Without
            # this, the video load falls back to `default-src 'self'`
            # and is blocked the same way the image was in 6ecb815.
            csp = csp.replace(
                "media-src 'self'",
                f"media-src 'self' {s3_origin}",
            )
        response.headers["Content-Security-Policy"] = csp
    return response


def _derive_s3_origin() -> str:
    """Return the origin (scheme + host + port) of the S3 endpoint the
    Flask /api/media/<key> proxy redirects to.

    Resolution order matches `s3._s3_client()`:

      1. `AWS_S3_ENDPOINT_URL` (explicit, e.g. ``http://localhost:9000``
         for local MinIO) — return its scheme + netloc.
      2. No explicit endpoint → real AWS. boto3 signs with the
         virtual-hosted-style URL
         ``https://<bucket>.s3.<region>.amazonaws.com``. Build the
         same shape from `AWS_S3_BUCKET` + `AWS_S3_REGION`.
      3. **`us-east-1` is special-cased** to the legacy global endpoint
         ``https://<bucket>.s3.amazonaws.com`` (no region in the host).
         boto3's default signing URL for us-east-1 buckets is the
         legacy form even with virtual-host addressing; building the
         regional form ``https://<bucket>.s3.us-east-1.amazonaws.com``
         here would put a CSP allow-list entry on an origin boto3
         never actually signs, and the browser blocks the load. (The
         regional form is also valid, but unused by our pipeline.)
      4. Missing region → legacy global form (same rationale: that's
         what an unconfigured boto3 client signs in us-east-1).

    Returns an empty string when the origin can't be determined
    (the CSP code treats that as "leave img-src alone"). Never
    raises — the CSP code runs in a hot request path.
    """
    try:
        from urllib.parse import urlparse

        endpoint = _cfg.if_exists("AWS_S3_ENDPOINT_URL")
        if endpoint:
            parsed = urlparse(endpoint)
            if parsed.scheme and parsed.netloc:
                return f"{parsed.scheme}://{parsed.netloc}"
            return ""
        bucket = _cfg.if_exists("AWS_S3_BUCKET")
        region = _cfg.if_exists("AWS_S3_REGION")
        # Resolve to the actual signed URL origin boto3 will produce.
        # `us-east-1` (and missing region, which boto3 also defaults to
        # the us-east-1 namespace) → legacy global endpoint.
        if bucket and (not region or region == "us-east-1"):
            return f"https://{bucket}.s3.amazonaws.com"
        if bucket and region:
            return f"https://{bucket}.s3.{region}.amazonaws.com"
        return ""
    except Exception:
        return ""


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
            # Status topic — separate from MQTT_TOPIC, used for the
            # sign's periodic health snapshot. sign_status.js reads
            # this from window.APP_CONFIG.mqttStatusTopic to open a
            # second MQTT-WS connection for the status flow. The
            # resolution happens server-side (default: "{MQTT_TOPIC}-status")
            # so the browser never has to derive it.
            "MQTT_STATUS_TOPIC": _mqtt_status_topic,
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
