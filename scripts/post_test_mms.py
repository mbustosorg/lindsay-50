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
import os
import sys
import tomllib
import urllib.parse
from http.cookiejar import CookieJar
from pathlib import Path
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

# Path to the repo's `heart-message-manager/settings.toml` — used
# as the default source of admin credentials and port when the
# caller doesn't pass --username/--password/--base-url explicitly.
# Resolves relative to the repo root (the script's parent's parent
# = the `lindsay-50/` checkout).
_REPO_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_SETTINGS = _REPO_ROOT / "heart-message-manager" / "settings.toml"


def _load_settings(path: Path) -> dict:
    """Best-effort TOML load. Returns an empty dict when the file
    is missing (the operator may not have one set up locally) or
    unparseable — caller treats empty as "no defaults available".
    """
    try:
        with path.open("rb") as f:
            return tomllib.load(f)
    except FileNotFoundError:
        return {}
    except tomllib.TOMLDecodeError as e:
        print(f"warning: could not parse {path}: {e}", file=sys.stderr)
        return {}


def _settings_defaults() -> tuple[str, str, str]:
    """Read admin username/password + server port from
    `heart-message-manager/settings.toml`. Returns
    `(username, password, port)` defaults that the caller can
    pass through to argparse.

    The file's `[auth]` table carries ADMIN_USERNAME /
    ADMIN_PASSWORD; the top-level `PORT` is the Flask server's
    listen port. Anything missing yields an empty string, which
    argparse translates to "use the argparse default".
    """
    cfg = _load_settings(_DEFAULT_SETTINGS)
    auth = cfg.get("auth", {})
    username = str(auth.get("ADMIN_USERNAME", "") or "")
    password = str(auth.get("ADMIN_PASSWORD", "") or "")
    port = str(cfg.get("PORT", "") or "")
    return username, password, port


def _build_opener(base_url: str, username: str, password: str) -> OpenerDirector:
    """Log in via /login (form post) and return an opener with the
    session cookie attached. Avoids pulling in `requests` — keeps
    the script stdlib-only so it runs in any venv.

    Raises `SystemExit(1)` if the login fails (the server returns
    the login page HTML with status 200 on bad creds — we detect
    the failure by checking that the post-login URL is NOT
    `/login` anymore).
    """
    cookie_jar = CookieJar()
    opener = build_opener(HTTPCookieProcessor(cookie_jar))

    login_url = f"{base_url}/login"
    login_body = urllib.parse.urlencode({"username": username, "password": password}).encode("ascii")
    login_req = Request(login_url, data=login_body, method="POST")
    with opener.open(login_req, timeout=10) as resp:
        # Flask returns 302 → / on success and the opener follows it,
        # so `resp.url` ends up at the dashboard. On failure Flask
        # returns 200 with the login HTML, and `resp.url` stays at
        # /login — that's our auth-failed signal.
        final_url = resp.url
        resp.read()

    if final_url.rstrip("/") == f"{base_url.rstrip('/')}/login":
        print(
            f"login FAILED for user={username!r} against {login_url}",
            file=sys.stderr,
        )
        print(
            "  -> server stayed on /login, meaning the credentials were rejected.",
            file=sys.stderr,
        )
        print(
            "  -> check ADMIN_USERNAME / ADMIN_PASSWORD env vars on the server,",
            file=sys.stderr,
        )
        print(
            "  -> or pass --username / --password to match.",
            file=sys.stderr,
        )
        sys.exit(1)

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
        final_url = resp.url
        resp_body = resp.read().decode("utf-8", errors="replace")
    print(f"POST /api/test-messages -> {status}  (final url: {final_url})")
    print(f"response body: {resp_body[:200]}")
    # Detect "auth redirected me to /login" — the route returns 302
    # and urllib follows it; final_url ends up at /login. The body
    # is the login HTML, not the TwiML `<Response>` we'd see on a
    # successful ingest. Flag it loudly so the operator doesn't
    # have to grep for the <title>Login</title> hint themselves.
    if final_url.rstrip("/").endswith("/login"):
        print(
            "  -> looks like the session cookie wasn't accepted; /api/test-messages redirected to /login.",
            file=sys.stderr,
        )
        print("  -> re-check ADMIN_USERNAME / ADMIN_PASSWORD on the server.", file=sys.stderr)
        sys.exit(1)
    # The success response is TwiML XML: <Response><Message>...</Message></Response>.
    # If we see <html in the body, it's almost certainly a redirect-to-login
    # or an error page — bail loudly so the operator notices.
    if "<html" in resp_body.lower() or "<!doctype" in resp_body.lower():
        print(
            "  -> response body looks like HTML, not TwiML — the server did not accept the test MMS.",
            file=sys.stderr,
        )
        sys.exit(1)
    if status >= 400:
        sys.exit(1)


def main(argv: list[str] | None = None) -> None:
    doc_first_line = __doc__.splitlines()[0] if __doc__ else "Post a fake Twilio MMS for local testing."

    # Pull admin creds + port from `heart-message-manager/settings.toml`
    # when available. Anything missing falls back to the canonical
    # defaults below. Env vars (e.g. ADMIN_USERNAME) ALWAYS win over
    # settings.toml, mirroring the server's own precedence rules —
    # the operator who started the server with env vars is the same
    # operator running this script.
    toml_user, toml_pass, toml_port = _settings_defaults()
    env_user = os.environ.get("ADMIN_USERNAME", "")
    env_pass = os.environ.get("ADMIN_PASSWORD", "")
    default_user = env_user or toml_user or "admin"
    default_pass = env_pass or toml_pass or "secret123"
    if toml_port:
        default_base = f"http://localhost:{toml_port}"
    else:
        default_base = "http://localhost:3100"

    parser = argparse.ArgumentParser(description=doc_first_line)
    parser.add_argument(
        "mode",
        choices=("image", "video", "both"),
        help="Which attachment(s) to include in the test MMS.",
    )
    parser.add_argument(
        "--base-url",
        default=default_base,
        help=(
            "Base URL of the running Flask app. " f"Default: {default_base} (settings.toml PORT={toml_port or '3100'})."
        ),
    )
    parser.add_argument(
        "--username",
        default=default_user,
        help=(
            "Admin username. Precedence: --username > $ADMIN_USERNAME > "
            f"settings.toml [auth].ADMIN_USERNAME > 'admin'. Resolved: {default_user!r}."
        ),
    )
    parser.add_argument(
        "--password",
        default=default_pass,
        help=(
            "Admin password. Same precedence as --username. " "Resolved value is intentionally not echoed in --help."
        ),
    )
    parser.add_argument(
        "--settings",
        type=Path,
        default=_DEFAULT_SETTINGS,
        help=("Path to settings.toml. " f"Default: {_DEFAULT_SETTINGS} (relative to the repo root)."),
    )
    args = parser.parse_args(argv)

    # If the caller overrode --settings, recompute from that path —
    # but only when the user did NOT also pass --username/--password/
    # --base-url explicitly. argparse already filled those from the
    # values above, so we need to know "did the user pass anything".
    # The simplest heuristic: re-resolve only when the values still
    # match the defaults (i.e. the user didn't override them). For
    # a smoke script this is good enough; the operator who passed
    # explicit flags is the operator who wins.
    if str(args.settings) != str(_DEFAULT_SETTINGS):
        toml_user, toml_pass, toml_port = _load_settings_from(args.settings)
        if args.username == default_user and env_user == "":
            args.username = toml_user or args.username
        if args.password == default_pass and env_pass == "":
            args.password = toml_pass or args.password
        if args.base_url == default_base and toml_port:
            args.base_url = f"http://localhost:{toml_port}"

    if args.mode == "image":
        attachments = ["image"]
    elif args.mode == "video":
        attachments = ["video"]
    else:
        attachments = ["image", "video"]

    print(f"posting test MMS: mode={args.mode} attachments={attachments}")
    print(f"  base_url={args.base_url}")
    print(f"  username={args.username!r}")
    print(f"  attachments={attachments}")
    post_test_mms(args.base_url, args.username, args.password, attachments)
    print("done. check /messages and the live ring buffer in the admin UI.")


def _load_settings_from(path: Path) -> tuple[str, str, str]:
    """Same shape as `_settings_defaults` but reads from an
    explicit path (so the operator can point at a non-default
    settings.toml)."""
    cfg = _load_settings(path)
    auth = cfg.get("auth", {})
    return (
        str(auth.get("ADMIN_USERNAME", "") or ""),
        str(auth.get("ADMIN_PASSWORD", "") or ""),
        str(cfg.get("PORT", "") or ""),
    )


if __name__ == "__main__":
    main()
