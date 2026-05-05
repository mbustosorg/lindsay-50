import os
import tomllib
from pathlib import Path
from flask import Flask, request, Response
import paho.mqtt.publish as publish

app = Flask(__name__)

# Load settings.toml (same file used by the ESP32/CircuitPython)
_toml_path = Path(__file__).parent.parent / "heart-matrix-controller" / "settings.toml"
with open(_toml_path, "rb") as f:
    _cfg = tomllib.load(f)

MQTT_HOST     = _cfg.get("MQTT_HOST", "localhost")
MQTT_PORT     = int(_cfg.get("MQTT_PORT", 1883))
MQTT_TOPIC    = _cfg.get("MQTT_TOPIC", "sms/incoming")
MQTT_USERNAME = _cfg.get("MQTT_USERNAME", "")
MQTT_PASSWORD = _cfg.get("MQTT_PASSWORD", "")
# HiveMQ Cloud (port 8883) requires TLS; auto-detect if not set explicitly
MQTT_TLS      = bool(_cfg.get("MQTT_TLS", MQTT_PORT == 8883))

# Optional: comma-separated list of allowed sender phone numbers, e.g. "+15551234567"
ALLOWED_SENDERS = [s for s in _cfg.get("ALLOWED_SENDERS", "").split(",") if s]


@app.route("/sms", methods=["POST"])
def sms_webhook():
    app.logger.debug("Headers: %s", dict(request.headers))
    app.logger.debug("Form data: %s", dict(request.form))

    sender = request.form.get("From", "")
    body   = request.form.get("Body", "").strip()

    app.logger.info("From=%r  Body=%r  ALLOWED_SENDERS=%r", sender, body, ALLOWED_SENDERS)

    if ALLOWED_SENDERS:
        if sender not in ALLOWED_SENDERS:
            app.logger.warning("Rejected SMS from %s", sender)
            return Response("Forbidden", status=403)

    if not body:
        return Response("", status=204)

    auth = None
    if MQTT_USERNAME:
        auth = {"username": MQTT_USERNAME, "password": MQTT_PASSWORD}

    tls = {} if not MQTT_TLS else {"ca_certs": None}   # set ca_certs path if needed

    try:
        publish.single(
            topic=MQTT_TOPIC,
            payload=body,
            hostname=MQTT_HOST,
            port=MQTT_PORT,
            auth=auth if auth else None,
            tls=tls if MQTT_TLS else None,
        )
        app.logger.info("Published to %s: %s", MQTT_TOPIC, body)
    except Exception as e:
        app.logger.error("MQTT publish failed: %s", e)
        return Response("Internal Server Error", status=500)

    reply = f"Lindsay's Heart got your message: {body}"
    return Response(f"<Response><Message>{reply}</Message></Response>", status=200, mimetype="text/xml")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
