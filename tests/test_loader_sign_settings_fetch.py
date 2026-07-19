"""Tests for the loader's /api/sign/settings path (issue #51 §5).

The loader's `fetch_target_version` wraps `lib_shared.boot_config.
fetch_sign_settings` — these tests cover the wrapping contract:

  - Returns the 7-char short SHA Flask resolves to.
  - Returns None on network / HTTP / JSON / missing-field errors.
  - The `force_upgrade_main` entrypoint bypasses AUTO_UPDATE.
  - The `main` entrypoint still respects AUTO_UPDATE.

Hermetic: the loader is loaded as a fresh module per test, the
loader module's `os.execvpe` is monkey-patched in the entrypoint
tests so we don't actually replace this process.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_PROJECT_ROOT = Path(__file__).parent.parent
_LOADER_PATH = _PROJECT_ROOT / "heart-matrix-controller" / "loader.py"


def _import_loader_fresh():
    spec = importlib.util.spec_from_file_location("hmc_loader_sign_settings_test", str(_LOADER_PATH))
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    sys.modules.setdefault("hmc_loader_under_test", mod)
    sys.modules.setdefault("loader", mod)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def loader():
    return _import_loader_fresh()


# ---------------------------------------------------------------------------
# fetch_target_version — wrapper around lib_shared.boot_config.fetch_sign_settings
# ---------------------------------------------------------------------------


class _FakeResponse:
    """Stand-in for a requests.Response — only what fetch_sign_settings reads."""

    def __init__(self, *, status_code=200, payload=None, raw_text=None):
        self.status_code = status_code
        self._payload = payload
        self._raw_text = raw_text

    def json(self):
        return self._payload

    @property
    def text(self):
        if self._raw_text is not None:
            return self._raw_text
        return json.dumps(self._payload)


class TestFetchTargetVersion:
    def test_returns_short_sha_on_200(self, loader):
        fake_requests = MagicMock()
        fake_requests.get.return_value = _FakeResponse(payload={"target_version": "abc1234", "version": 2})
        result = loader.fetch_target_version(
            api_url="https://x/api/messages",
            api_key="k",
            requests_module=fake_requests,
        )
        assert result == "abc1234"
        # Verify the call: GET to the /api/sign/settings origin, X-API-Key header.
        called = fake_requests.get.call_args
        assert "/api/sign/settings" in called.args[0]
        assert called.kwargs["headers"]["X-API-Key"] == "k"

    def test_returns_none_on_non_200(self, loader):
        fake_requests = MagicMock()
        fake_requests.get.return_value = _FakeResponse(status_code=503)
        result = loader.fetch_target_version(
            api_url="https://x/api/messages",
            api_key="k",
            requests_module=fake_requests,
        )
        assert result is None

    def test_returns_none_on_network_error(self, loader):
        fake_requests = MagicMock()
        fake_requests.get.side_effect = ConnectionError("broker down")
        result = loader.fetch_target_version(
            api_url="https://x/api/messages",
            api_key="k",
            requests_module=fake_requests,
        )
        assert result is None

    def test_returns_none_on_missing_target_version(self, loader):
        fake_requests = MagicMock()
        fake_requests.get.return_value = _FakeResponse(
            payload={"version": 2, "filters": [], "senders": []},
        )
        result = loader.fetch_target_version(
            api_url="https://x/api/messages",
            api_key="k",
            requests_module=fake_requests,
        )
        assert result is None

    def test_short_sha_form_matches_new_wire_contract(self, loader):
        """The wire form is a 7-char short SHA — never a full SHA, never None.

        Mirrors the GET /api/sign/settings endpoint shape; tests the
        loader-side guarantee that we pass the same form through.
        """
        fake_requests = MagicMock()
        fake_requests.get.return_value = _FakeResponse(
            payload={
                "target_version": "b5e191c",
                "version": 2,
            },
        )
        result = loader.fetch_target_version(
            api_url="https://x/api/messages",
            api_key="k",
            requests_module=fake_requests,
        )
        assert result == "b5e191c"
        assert len(result) == 7


# ---------------------------------------------------------------------------
# force_upgrade_main — bypasses AUTO_UPDATE
# ---------------------------------------------------------------------------


def _write_minimal_loader_workspace(repo_dir: Path) -> None:
    """Create a git workspace that the loader entrypoint can probe.

    Two commits, with the bare repo as a `current` symlink target —
    enough to satisfy `git rev-parse HEAD` from `current` (the loader
    resolves local SHA there) and to satisfy any `git fetch` no-op.
    """
    repo_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "-C", str(repo_dir), "init", "--initial-branch=main"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo_dir), "config", "user.email", "test@example.com"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo_dir), "config", "user.name", "Test"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo_dir), "config", "commit.gpgsign", "false"],
        check=True,
        capture_output=True,
    )
    (repo_dir / "README.md").write_text("first\n")
    subprocess.run(
        ["git", "-C", str(repo_dir), "add", "README.md"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo_dir), "commit", "-m", "first"],
        check=True,
        capture_output=True,
    )


class TestForceUpgradeMain:
    def test_bypasses_auto_update_off(self, loader, tmp_path, monkeypatch):
        """`AUTO_UPDATE=false` in config but `force_upgrade_main` still runs
        the upgrade flow — that's the whole point of the entrypoint."""
        _write_minimal_loader_workspace(tmp_path)
        # Stub config_reader: AUTO_UPDATE deliberately false.
        cfg = MagicMock()
        cfg.if_exists.side_effect = lambda k: {
            "CONFIG_API_URL": "https://x/api/messages",
            "API_SECRET_KEY": "k",
            "AUTO_UPDATE": "false",
        }.get(k)
        fake_get_config = MagicMock(return_value=cfg)

        captured = {}

        def fake_run_upgrade_flow(repo_dir, *, api_url, api_key, **_kw):
            captured["called"] = True
            captured["api_url"] = api_url
            captured["api_key"] = api_key
            captured["repo_dir"] = repo_dir

        monkeypatch.setitem(sys.modules, "lib_shared.config_reader", MagicMock())
        sys.modules["lib_shared.config_reader"].get_config = fake_get_config
        monkeypatch.setattr(loader, "run_upgrade_flow", fake_run_upgrade_flow)
        # Prevent the actual exec_active fallback at end of force_upgrade_main.
        monkeypatch.setattr(loader, "exec_active", lambda *a, **kw: None)

        monkeypatch.setenv("LINDSAY50_FORCE_UPGRADE", "1")
        monkeypatch.setenv("LINDSAY50_REPO_DIR", str(tmp_path))

        result = loader.force_upgrade_main()
        assert result == 0
        assert captured["called"] is True
        assert captured["api_url"] == "https://x/api/messages"
        assert captured["api_key"] == "k"
        assert captured["repo_dir"] == tmp_path

    def test_falls_through_to_existing_current_when_config_missing(self, loader, tmp_path, monkeypatch):
        """No config — even force_upgrade can't proceed. Falls through to
        running whatever's already in `current/`. The Pi can't brick itself
        by clicking the button."""
        monkeypatch.setattr(loader, "exec_active", lambda *a, **kw: None)
        # Make `from lib_shared.config_reader import get_config` fail.
        monkeypatch.delitem(sys.modules, "lib_shared.config_reader", raising=False)
        monkeypatch.setenv("LINDSAY50_REPO_DIR", str(tmp_path))

        result = loader.force_upgrade_main()
        assert result == 0

    def test_falls_through_when_api_url_missing(self, loader, tmp_path, monkeypatch):
        monkeypatch.setattr(loader, "exec_active", lambda *a, **kw: None)
        cfg = MagicMock()
        cfg.if_exists.side_effect = lambda k: {
            "CONFIG_API_URL": "",
            "API_SECRET_KEY": "k",
        }.get(k)
        fake_get_config = MagicMock(return_value=cfg)
        monkeypatch.setitem(sys.modules, "lib_shared.config_reader", MagicMock())
        sys.modules["lib_shared.config_reader"].get_config = fake_get_config
        monkeypatch.setenv("LINDSAY50_REPO_DIR", str(tmp_path))

        result = loader.force_upgrade_main()
        assert result == 0


class TestMainDispatchesOnForceUpgradeEnvVar:
    """`main()` routes to `force_upgrade_main` when LINDSAY50_FORCE_UPGRADE=1."""

    def test_dispatches_when_env_set(self, loader, monkeypatch):
        monkeypatch.setattr(loader, "force_upgrade_main", MagicMock(return_value=42))
        monkeypatch.setattr(loader, "exec_active", lambda *a, **kw: None)
        monkeypatch.setenv("LINDSAY50_FORCE_UPGRADE", "1")

        result = loader.main()
        assert result == 42
        loader.force_upgrade_main.assert_called_once()

    def test_dispatches_strips_whitespace(self, loader, monkeypatch):
        monkeypatch.setattr(loader, "force_upgrade_main", MagicMock(return_value=0))
        monkeypatch.setattr(loader, "exec_active", lambda *a, **kw: None)
        monkeypatch.setenv("LINDSAY50_FORCE_UPGRADE", "  1 \n")

        result = loader.main()
        assert result == 0
        loader.force_upgrade_main.assert_called_once()

    def test_does_not_dispatch_when_env_var_unset(self, loader, monkeypatch):
        """No LINDSAY50_FORCE_UPGRADE → fall through to the normal main() path."""
        monkeypatch.delenv("LINDSAY50_FORCE_UPGRADE", raising=False)
        monkeypatch.setattr(loader, "force_upgrade_main", MagicMock())
        monkeypatch.setattr(loader, "exec_active", lambda *a, **kw: None)

        # Force the normal-path fallthrough: AUTO_UPDATE off, missing api.
        cfg = MagicMock()
        cfg.if_exists.side_effect = lambda k: {
            "CONFIG_API_URL": "",
            "API_SECRET_KEY": "",
            "AUTO_UPDATE": "false",
        }.get(k)
        fake_get_config = MagicMock(return_value=cfg)
        monkeypatch.setitem(sys.modules, "lib_shared.config_reader", MagicMock())
        sys.modules["lib_shared.config_reader"].get_config = fake_get_config

        loader.main()
        loader.force_upgrade_main.assert_not_called()


# ---------------------------------------------------------------------------
# _resolve_full_sha — git rev-parse contract
# ---------------------------------------------------------------------------


class TestResolveFullSha:
    def test_returns_full_sha_for_short_input(self, loader, tmp_path):
        """Resolves a 7-char short SHA to its full 40-char form."""
        _write_minimal_loader_workspace(tmp_path)
        short = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "--short=7", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        result = loader._resolve_full_sha(tmp_path, short)
        assert result is not None
        assert len(result) == 40
        assert result.startswith(short)

    def test_returns_short_input_on_rev_parse_failure(self, loader, tmp_path):
        """git rev-parse fails → None returned (caller falls through)."""
        # Empty directory → no commits → rev-parse fails.
        result = loader._resolve_full_sha(tmp_path, "deadbee")
        assert result is None
