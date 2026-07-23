"""Authentication module for heart-message-manager.

Provides browser session auth (Flask-Login) and API key auth (X-API-Key header)
for ESP32 machine clients.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from flask import Blueprint, redirect, request, session, url_for
from flask_login import LoginManager, UserMixin, login_required, login_user, logout_user

if TYPE_CHECKING:
    from flask import Flask

auth_bp = Blueprint("auth", __name__)

login_manager = LoginManager()


class AuthUser(UserMixin):
    """Flask-Login user wrapper. id is always "admin" for the single shared credential."""

    def __init__(self, id: str) -> None:
        self.id = id


@login_manager.user_loader
def load_user(user_id: str) -> AuthUser | None:
    if user_id == "admin":
        return AuthUser(id="admin")
    return None


# ---------------------------------------------------------------------------
# Sliding session expiration
# ---------------------------------------------------------------------------


def _check_session_timeout() -> None:
    """Clear session if sliding inactivity timeout has expired.

    Called on every request via before_request in init_app.
    """
    last_activity = session.get("_last_activity")
    if last_activity is None:
        return
    timeout_mins = session.get("_timeout_mins", 60)
    if time.time() - last_activity > timeout_mins * 60:
        logout_user()
        session.clear()


@auth_bp.before_app_request
def before_request() -> None:
    """Check API key auth or session auth before every request."""
    from flask import g

    # Skip auth for routes that are always open
    if request.endpoint in ("health", "auth.login", "auth.logout"):
        return

    # API key auth (ESP32 machine clients)
    api_key = request.headers.get("X-API-Key")
    if api_key is not None:
        from lib_shared.config_reader import get_config

        try:
            cfg = get_config(
                {
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
            )
        except KeyError:
            pass  # config not ready yet, fall through

        try:
            stored_key = cfg.if_exists("API_SECRET_KEY")
            if stored_key and api_key == stored_key:
                g.api_key_auth = True
                return
            else:
                g.api_key_auth = False
        except Exception:
            pass

    # Browser session auth — check sliding timeout
    _check_session_timeout()


def _set_session_timeout(timeout_mins: int) -> None:
    """Store timeout value in session for sliding expiration check."""
    session["_timeout_mins"] = timeout_mins


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    """GET: render login form. POST: validate credentials and log in."""
    from flask import flash, render_template

    if request.method == "GET":
        return render_template("login.html")

    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")

    if not username or not password:
        flash("Please enter both username and password.", "error")
        return render_template("login.html"), 200

    # Validate against config
    from lib_shared.config_reader import get_config

    try:
        cfg = get_config(
            {
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
        )
    except KeyError:
        flash("Server configuration error.", "error")
        return render_template("login.html"), 500

    stored_username = cfg.if_exists("ADMIN_USERNAME")
    stored_password = cfg.if_exists("ADMIN_PASSWORD")

    if stored_username and stored_password and username == stored_username and password == stored_password:
        timeout_mins = 60
        try:
            timeout_mins = int(cfg.if_exists("ADMIN_SESSION_TIMEOUT_MINS") or "60")
        except ValueError:
            pass

        session["_last_activity"] = time.time()
        _set_session_timeout(timeout_mins)
        login_user(AuthUser(id="admin"))
        next_url = request.args.get("next", url_for("dashboard"))
        # Note: an earlier implementation appended `?wipe=1` here so
        # the client-side app would wipe its IndexedDB buffer on
        # login. Issue #48 dropped IndexedDB persistence (the message
        # buffer is now per-tab in-memory only, owned by the
        # per-generation MessageManager), so the wipe flag is dead.
        # Login lands the operator on a fresh browser context
        # regardless.
        return redirect(next_url)

    flash("Invalid username or password.", "error")
    return render_template("login.html"), 200


@auth_bp.route("/logout")
@login_required
def logout():
    """Clear session and redirect to login."""
    logout_user()
    session.clear()
    return redirect(url_for("auth.login"))


# ---------------------------------------------------------------------------
# Init
# ---------------------------------------------------------------------------


def init_app(app: Flask) -> None:
    """Register auth blueprint and login manager with the Flask app."""
    login_manager.init_app(app)
    login_manager.login_view = "auth.login"  # type: ignore[attr-defined]  # Flask-Login stubs typo: login_view is read-only in stubs but writable at runtime
    login_manager.login_message = "Please log in to access this page."  # type: ignore[attr-defined]  # same Flask-Login stubs limitation
    app.register_blueprint(auth_bp)

    # Update last activity on every request after auth check
    @app.after_request
    def _update_last_activity(response):  # type: ignore  # Flask registers this via @after_request; never called by name
        if session.get("_last_activity") is not None:
            session["_last_activity"] = time.time()
        return response
