import html
import tomllib
from pathlib import Path
from flask import Flask, request, Response
from adafruit_io import Client

app = Flask(__name__)

with open(Path(__file__).parent / "settings.toml", "rb") as f:
    _cfg = tomllib.load(f)

AIO_USERNAME = _cfg["AIO_USERNAME"]
AIO_KEY = _cfg["AIO_KEY"]
AIO_FEED = _cfg["AIO_FEED"]

ALLOWED_SENDERS = [s.strip() for s in _cfg.get("ALLOWED_SENDERS", "").split(",") if s.strip()]

aio = Client(AIO_USERNAME, AIO_KEY)


@app.route("/sms", methods=["POST"])
def sms_webhook():
    sender = request.form.get("From", "")
    body = request.form.get("Body", "").strip()

    app.logger.info("From=%r Body=%r", sender, body)

    if ALLOWED_SENDERS and sender not in ALLOWED_SENDERS:
        app.logger.warning("Rejected SMS from %s", sender)
        return Response("Forbidden", status=403)

    if not body:
        return Response("", status=204)

    try:
        aio.send_data(AIO_FEED, body)
        app.logger.info("Published to feed %s: %s", AIO_FEED, body)
    except Exception as e:
        app.logger.error("Adafruit IO publish failed: %s", e)
        return Response("Internal Server Error", status=500)

    reply = f"Lindsay's Heart got your message: {html.escape(body)}"
    return Response(
        f"<Response><Message>{reply}</Message></Response>",
        status=200,
        mimetype="text/xml",
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
