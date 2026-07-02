"""Tests for the app-side `check_for_update` handler.

The handler runs on the Pi (registered with `MessageManager`) and
is invoked when Flask publishes a `command=check-for-update`
MQTT envelope at startup. It compares the expected SHA from
Flask to the running SHA (set by the loader via the
`LINDSAY50_ACTIVE_SHA` env var) and `os.execvpe`s into the
loader if they differ.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from check_for_update import (
    ENV_ACTIVE_SHA,
    ENV_BOOT_ID,
    ENV_REPO_DIR,
    LOADER_PATH,
    _exec_into_loader,
    _resolve_active_sha,
    _resolve_repo_dir,
    check_for_update,
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):  # noqa: D401 — fixture
    """Strip LINDSAY50_* vars so tests don't leak between cases."""
    _ = _clean_env  # silence Pyright "not accessed" on the fixture
    for var in (ENV_ACTIVE_SHA, ENV_REPO_DIR, ENV_BOOT_ID):
        monkeypatch.delenv(var, raising=False)


class TestResolveActiveSha:
    def test_returns_value_when_set(self, monkeypatch):
        monkeypatch.setenv(ENV_ACTIVE_SHA, "abcdef")
        assert _resolve_active_sha() == "abcdef"

    def test_returns_none_when_unset(self):
        assert _resolve_active_sha() is None

    def test_returns_none_when_empty(self, monkeypatch):
        monkeypatch.setenv(ENV_ACTIVE_SHA, "")
        assert _resolve_active_sha() is None

    def test_returns_none_when_whitespace_only(self, monkeypatch):
        monkeypatch.setenv(ENV_ACTIVE_SHA, "   ")
        assert _resolve_active_sha() is None


class TestResolveRepoDir:
    def test_uses_env_var_when_set(self, monkeypatch, tmp_path):
        monkeypatch.setenv(ENV_REPO_DIR, str(tmp_path))
        assert _resolve_repo_dir() == tmp_path

    def test_falls_back_to_conventional_pi_path(self, monkeypatch):
        monkeypatch.delenv(ENV_REPO_DIR, raising=False)
        result = _resolve_repo_dir()
        assert isinstance(result, Path)
        # The default points at the conventional Pi path; we don't
        # require the path to exist (test env may not have it).
        assert str(result).endswith("/home/pi/projects/lindsay-50")


class TestCheckForUpdate:
    def test_no_op_when_active_sha_missing(self, monkeypatch):
        """Without LINDSAY50_ACTIVE_SHA, we don't know what we're running.
        Skipping the exec is the safe choice."""
        monkeypatch.delenv(ENV_ACTIVE_SHA, raising=False)
        with patch("check_for_update.os.execvpe") as mock_exec:
            check_for_update(api_url="https://x/api/messages", api_key="k")
        mock_exec.assert_not_called()

    def test_no_op_when_fetch_fails(self, monkeypatch):
        """If we can't reach Flask, we don't have an expected SHA to compare.
        Skip the exec."""
        monkeypatch.setenv(ENV_ACTIVE_SHA, "old-sha")
        with patch("check_for_update.fetch_boot_config", return_value=None):
            with patch("check_for_update.os.execvpe") as mock_exec:
                check_for_update(api_url="https://x/api/messages", api_key="k")
        mock_exec.assert_not_called()

    def test_no_op_when_sha_matches(self, monkeypatch):
        """If expected == active, no upgrade needed."""
        monkeypatch.setenv(ENV_ACTIVE_SHA, "same-sha")
        bc_mock = MagicMock()
        bc_mock.expected_sha = "same-sha"
        with patch("check_for_update.fetch_boot_config", return_value=bc_mock):
            with patch("check_for_update.os.execvpe") as mock_exec:
                check_for_update(api_url="https://x/api/messages", api_key="k")
        mock_exec.assert_not_called()

    def test_execs_loader_on_mismatch(self, monkeypatch, tmp_path):
        """If expected != active, exec into the loader with env vars set."""
        monkeypatch.setenv(ENV_ACTIVE_SHA, "old-sha")
        monkeypatch.setenv(ENV_REPO_DIR, str(tmp_path))
        bc_mock = MagicMock()
        bc_mock.expected_sha = "new-sha"
        with patch("check_for_update.fetch_boot_config", return_value=bc_mock):
            with patch("check_for_update.os.execvpe") as mock_exec:
                check_for_update(api_url="https://x/api/messages", api_key="k")
        mock_exec.assert_called_once()
        # os.execvpe(file, args, env) is called positionally.
        args = mock_exec.call_args.args
        # args[0] is the python executable, args[1] is the argv list,
        # args[2] is the env dict.
        argv = args[1]
        loader_arg = argv[1]
        assert loader_arg.endswith(str(LOADER_PATH))
        env_dict: dict = args[2]
        assert env_dict[ENV_ACTIVE_SHA] == "new-sha"
        assert env_dict[ENV_REPO_DIR] == str(tmp_path)

    def test_uses_explicit_repo_dir_override(self, monkeypatch):
        """repo_dir kwarg overrides LINDSAY50_REPO_DIR env var when computing the loader path."""
        monkeypatch.setenv(ENV_ACTIVE_SHA, "old")
        monkeypatch.setenv(ENV_REPO_DIR, "/some/other/path")
        bc_mock = MagicMock()
        bc_mock.expected_sha = "new"
        with patch("check_for_update.fetch_boot_config", return_value=bc_mock):
            with patch("check_for_update.os.execvpe") as mock_exec:
                check_for_update(
                    api_url="https://x/api/messages",
                    api_key="k",
                    repo_dir=Path("/explicit/path"),
                )
        # os.execvpe(file, args, env) — the loader path is in argv[1].
        argv = mock_exec.call_args.args[1]
        loader_arg = argv[1]
        # The loader path is /explicit/path/current/heart-matrix-controller/loader.py
        # (the repo_dir kwarg was used, not the env var).
        assert loader_arg.startswith("/explicit/path/")
        assert loader_arg.endswith(str(LOADER_PATH))

    def test_passes_api_url_and_key_to_fetch(self, monkeypatch):
        """Forward api_url and api_key to fetch_boot_config."""
        monkeypatch.setenv(ENV_ACTIVE_SHA, "old")
        bc_mock = MagicMock()
        bc_mock.expected_sha = "old"  # match — no exec
        with patch("check_for_update.fetch_boot_config", return_value=bc_mock) as mock_fetch:
            check_for_update(api_url="https://x/api/messages", api_key="my-key")
        assert mock_fetch.call_args.kwargs["api_url"] == "https://x/api/messages"
        assert mock_fetch.call_args.kwargs["api_key"] == "my-key"


class TestExecIntoLoader:
    def test_exec_sets_active_sha(self, monkeypatch, tmp_path):
        """_exec_into_loader sets LINDSAY50_ACTIVE_SHA to the expected SHA."""
        with patch("check_for_update.os.execvpe") as mock_exec:
            _exec_into_loader(tmp_path, "newsha")
        env: dict = mock_exec.call_args.args[2]
        assert env[ENV_ACTIVE_SHA] == "newsha"

    def test_exec_inherits_existing_env_vars(self, monkeypatch, tmp_path):
        """_exec_into_loader's env dict inherits os.environ (so boot_id, etc. carry over)."""
        _ = tmp_path  # silence Pyright "monkeypatch only" — keep signature stable
        monkeypatch.setenv(ENV_BOOT_ID, "boot-123")
        with patch("check_for_update.os.execvpe") as mock_exec:
            _exec_into_loader(tmp_path, "newsha")
        env: dict = mock_exec.call_args.args[2]
        # boot_id was inherited from os.environ via the env= dict.
        assert env.get(ENV_BOOT_ID) == "boot-123"

    def test_exec_loader_path_resolves_through_repo(self, tmp_path):
        """_exec_into_loader builds the loader path as repo_dir/current/.../loader.py."""
        with patch("check_for_update.os.execvpe") as mock_exec:
            _exec_into_loader(tmp_path, "newsha")
        argv = mock_exec.call_args.args[1]
        loader_arg = argv[1]
        assert loader_arg == str(tmp_path / LOADER_PATH)
