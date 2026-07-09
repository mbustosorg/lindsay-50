"""Post a fake Twilio MMS to a locally-running heart-message-manager.

Bypasses the Twilio signature path entirely (the test injection
endpoint `/api/test-messages` requires only an admin session, not
a Twilio Auth Token). The server still exercises the full MMS
ingest pipeline: form parsing → async background download →
S3 copy → SQLite row → MQTT envelope publish → admin UI thumbnail.

Three modes (set via the first CLI arg, or pick from the menu):

    python scripts/post_test_mms.py image
    python scripts/post_test_mms.py video
    python scripts/post_test_mms.py both

The PUBLIC_MEDIA dict below uses real, deterministic, public
HTTPS URLs so the Flask handler's `requests.get(MediaUrl0)` can
actually fetch the bytes. picsum's /id/<n> path is stable
(the same `id` always returns the same image) so the URL in the
MQTT envelope and SQLite row is the same one you'd see in
production. w3schools' `mov_bbb.mp4` is a 1-second H.264 sample.

Usage:
    # 1. Start the server with admin creds you know:
    ADMIN_USERNAME=admin ADMIN_PASSWORD=secret123 \
        python heart-message-manager/main.py

    # 2. In another terminal, post:
    python scripts/post_test_mms.py image
    python scripts/post_test_mms.py video
    python scripts/post_test_mms.py both

    # 3. Watch the admin UI at http://localhost:3100/messages
    #    and the live ring buffer at http://localhost:3100/
"""

from __future__ import annotations

import argparse
import sys
import urllib.parse
from http.cookiejar import CookieJar
from urllib.request import HTTPCookieProcessor, OpenerDirector, Request, build_opener

# Real, stable, public HTTPS URLs. picsum redirects 302 to a
# deterministic `fastly.picsum.photos/id/<n>` URL — `requests`
# follows the redirect by default so the Flask handler downloads
# the actual image bytes (not the redirect HTML page).
PUBLIC_MEDIA: dict[str, dict[str, str]] = {
    "image": {
        "type": "image/jpeg",
        # 256x256 deterministic JPEG; small enough to fetch fast.
        "url": "https://picsum.photos/id/1015/256/256.jpg",
    },
    "video": {
        "type": "video/mp4",
        # W3Schools sample clip — ~1s H.264 baseline MP4, ~770 KB.
        "url": "https://www.w3schools.com/html/mov_bbb.mp4",
    },
}


def _build_opener(base_url: str, username: str, password: str) -> OpenerDirector:
    """Log in via /login (form post) and return an opener with the
    session cookie attached. Avoids pulling in `requests` — keeps
    the script stdlib-only so it runs in any venv.
    """
    cookie_jar = CookieJar()
    opener = build_opener(HTTPCookieProcessor(cookie_jar))

    login_url = f"{base_url}/login"
    login_body = urllib.parse.urlencode({"username": username, "password": password}).encode("ascii")
    login_req = Request(login_url, data=login_body, method="POST")
    with opener.open(login_req, timeout=10) as resp:
        # Flask redirects to `/` on success (302). The opener follows
        # the redirect; we don't need the response body.
        resp.read()

    return opener


def post_test_mms(base_url: str, username: str, password: str, attachments: list[str]) -> None:
    """Build a Twilio-shaped form body and POST it to
    `/api/test-messages`. The handler parses `NumMedia` + the
    indexed `MediaContentType{i}` / `MediaUrl{i}` fields and
    kicks off the same async MMS ingest as the real Twilio
    webhook."""
    opener = _build_opener(base_url, username, password)

    # Twilio sends fields as application/x-www-form-urlencoded
    # POST body — same shape as the real webhook.
    fields: dict[str, str] = {
        "From": "+15551234567",
        "To": "+15559999999",
        "Body": f"test mms ({','.join(attachments)})",
        # Bypass the dedupe-guard. Twilio webhooks include a SID
        # but our test injection should always succeed even if
        # the same body was posted before.
        "MessageSid": "",
    }
    for i, key in enumerate(attachments):
        media = PUBLIC_MEDIA[key]
        fields[f"MediaContentType{i}"] = media["type"]
        fields[f"MediaUrl{i}"] = media["url"]
    fields["NumMedia"] = str(len(attachments))

    body = urllib.parse.urlencode(fields).encode("ascii")
    req = Request(
        f"{base_url}/api/test-messages",
        data=body,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with opener.open(req, timeout=15) as resp:
        status = resp.status
        resp_body = resp.read().decode("utf-8", errors="replace")
    print(f"POST /api/test-messages -> {status}")
    print(f"response body: {resp_body[:200]}")
    if status >= 400:
        sys.exit(1)


def main(argv: list[str] | None = None) -> None:
    doc_first_line = __doc__.splitlines()[0] if __doc__ else "Post a fake Twilio MMS for local testing."
    parser = argparse.ArgumentParser(description=doc_first_line)
    parser.add_argument(
        "mode",
        choices=("image", "video", "both"),
        help="Which attachment(s) to include in the test MMS.",
    )
    parser.add_argument(
        "--base-url",
        default="http://localhost:3100",
        help="Base URL of the running Flask app (default: http://localhost:3100).",
    )
    parser.add_argument(
        "--username",
        default="admin",
        help="Admin username (matches ADMIN_USERNAME env var; default: admin).",
    )
    parser.add_argument(
        "--password",
        default="secret123",
        help="Admin password (matches ADMIN_PASSWORD env var; default: secret123).",
    )
    args = parser.parse_args(argv)

    if args.mode == "image":
        attachments = ["image"]
    elif args.mode == "video":
        attachments = ["video"]
    else:
        attachments = ["image", "video"]

    print(f"posting test MMS: mode={args.mode} attachments={attachments}")
    print(f"  base_url={args.base_url}")
    print(f"  attachments={attachments}")
    post_test_mms(args.base_url, args.username, args.password, attachments)
    print("done. check /messages and the live ring buffer in the admin UI.")


if __name__ == "__main__":
    main()
