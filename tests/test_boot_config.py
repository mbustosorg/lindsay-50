"""Tests for `lib_shared/boot_config.py`.

Shared code used by Flask, the loader, and the app-side
`check_for_update` handler. Covers the dataclass parser, the
HTTP fetch (success / 401 / 500 / network error / malformed JSON),
the `current_sha` git helper, and the server-side
`from_heroku_or_git` SHA derivation.

These tests are hermetic — they inject a `requests` module mock
and a tmp git repo so no network or real git history is required.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import FrozenInstanceError
from unittest.mock import MagicMock

import pytest

from lib_shared.boot_config import (
    BOOT_CONFIG_PATH,
    SHORT_SHA_LEN,
    BootConfig,
    current_sha,
    fetch_boot_config,
    from_heroku_or_git,
    from_response,
    short_sha,
)


class TestBootConfigDataclass:
    def test_construction_and_immutability(self):
        bc = BootConfig(expected_sha="abc123")
        assert bc.expected_sha == "abc123"
        with pytest.raises(FrozenInstanceError):
            bc.expected_sha = "def456"  # type: ignore[misc]

    def test_construction_with_empty_sha(self):
        # Empty SHA is valid at the type level — the HTTP caller
        # treats it as "no answer"; the dataclass doesn't enforce
        # non-empty.
        bc = BootConfig(expected_sha="")
        assert bc.expected_sha == ""


class TestFromResponse:
    def test_parses_expected_sha(self):
        bc = from_response({"expected_sha": "abc123def"})
        assert bc == BootConfig(expected_sha="abc123def")

    def test_rejects_non_dict(self):
        assert from_response("not a dict") is None
        assert from_response([1, 2, 3]) is None
        assert from_response(None) is None

    def test_rejects_missing_key(self):
        assert from_response({}) is None
        assert from_response({"other_key": "x"}) is None

    def test_rejects_empty_sha(self):
        assert from_response({"expected_sha": ""}) is None

    def test_rejects_non_string_sha(self):
        assert from_response({"expected_sha": 123}) is None
        assert from_response({"expected_sha": None}) is None
        assert from_response({"expected_sha": ["abc"]}) is None


class TestFetchBootConfig:
    def test_returns_bootconfig_on_success(self):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"expected_sha": "newsha"}
        requests_module = MagicMock()
        requests_module.get.return_value = resp

        bc = fetch_boot_config(
            api_url="https://example.com/api/messages",
            api_key="secret",
            requests_module=requests_module,
        )
        assert bc == BootConfig(expected_sha="newsha")
        # Verify the URL was derived correctly and the auth header set.
        called_url = requests_module.get.call_args.args[0]
        assert called_url == f"https://example.com{BOOT_CONFIG_PATH}"
        assert called_url.endswith(BOOT_CONFIG_PATH)
        headers = requests_module.get.call_args.kwargs["headers"]
        assert headers == {"X-API-Key": "secret"}
        assert requests_module.get.call_args.kwargs["timeout"] == 5.0

    def test_returns_none_on_401(self):
        resp = MagicMock()
        resp.status_code = 401
        requests_module = MagicMock()
        requests_module.get.return_value = resp

        assert (
            fetch_boot_config(
                api_url="https://example.com/api/messages",
                api_key="bad",
                requests_module=requests_module,
            )
            is None
        )

    def test_returns_none_on_500(self):
        resp = MagicMock()
        resp.status_code = 500
        requests_module = MagicMock()
        requests_module.get.return_value = resp

        assert (
            fetch_boot_config(
                api_url="https://example.com/api/messages",
                api_key="secret",
                requests_module=requests_module,
            )
            is None
        )

    def test_returns_none_on_network_error(self):
        requests_module = MagicMock()
        requests_module.get.side_effect = ConnectionError("broker down")

        assert (
            fetch_boot_config(
                api_url="https://example.com/api/messages",
                api_key="secret",
                requests_module=requests_module,
            )
            is None
        )

    def test_returns_none_on_timeout(self):
        requests_module = MagicMock()
        requests_module.get.side_effect = TimeoutError("slow")

        assert (
            fetch_boot_config(
                api_url="https://example.com/api/messages",
                api_key="secret",
                requests_module=requests_module,
            )
            is None
        )

    def test_returns_none_on_malformed_json(self):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.side_effect = json.JSONDecodeError("bad", "raw", 0)
        requests_module = MagicMock()
        requests_module.get.return_value = resp

        assert (
            fetch_boot_config(
                api_url="https://example.com/api/messages",
                api_key="secret",
                requests_module=requests_module,
            )
            is None
        )

    def test_returns_none_on_missing_key(self):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"other": "x"}
        requests_module = MagicMock()
        requests_module.get.return_value = resp

        assert (
            fetch_boot_config(
                api_url="https://example.com/api/messages",
                api_key="secret",
                requests_module=requests_module,
            )
            is None
        )

    def test_returns_none_when_api_url_empty(self):
        assert (
            fetch_boot_config(
                api_url="",
                api_key="secret",
                requests_module=MagicMock(),
            )
            is None
        )

    def test_returns_none_when_api_key_empty(self):
        assert (
            fetch_boot_config(
                api_url="https://example.com/api/messages",
                api_key="",
                requests_module=MagicMock(),
            )
            is None
        )

    def test_returns_none_on_unparseable_url(self):
        assert (
            fetch_boot_config(
                api_url="not-a-url",
                api_key="secret",
                requests_module=MagicMock(),
            )
            is None
        )

    def test_respects_custom_timeout(self):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"expected_sha": "newsha"}
        requests_module = MagicMock()
        requests_module.get.return_value = resp

        fetch_boot_config(
            api_url="https://example.com/api/messages",
            api_key="secret",
            requests_module=requests_module,
            timeout=2.5,
        )
        assert requests_module.get.call_args.kwargs["timeout"] == 2.5


class TestCurrentSha:
    def test_returns_sha_when_current_symlink_valid(self, tmp_path):
        # Create a tmp git repo, point a `current` symlink at it,
        # confirm `current_sha` reads its HEAD.
        repo = tmp_path / "real-repo"
        repo.mkdir()
        subprocess.check_call(["git", "init", "--quiet", str(repo)])
        subprocess.check_call(["git", "-C", str(repo), "config", "user.email", "t@t.com"])
        subprocess.check_call(["git", "-C", str(repo), "config", "user.name", "T"])
        (repo / "f.txt").write_text("x")
        subprocess.check_call(["git", "-C", str(repo), "add", "f.txt"])
        subprocess.check_call(["git", "-C", str(repo), "commit", "-m", "i", "--quiet"])
        sha = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()

        (tmp_path / "current").symlink_to(repo)

        assert current_sha(tmp_path) == sha

    def test_returns_none_when_no_current_symlink(self, tmp_path):
        assert current_sha(tmp_path) is None

    def test_returns_none_when_current_is_broken_symlink(self, tmp_path):
        (tmp_path / "current").symlink_to(tmp_path / "does-not-exist")
        assert current_sha(tmp_path) is None

    def test_returns_none_when_git_fails(self, tmp_path):
        # `current` points at a directory with no git history.
        non_repo = tmp_path / "not-a-repo"
        non_repo.mkdir()
        (tmp_path / "current").symlink_to(non_repo)
        assert current_sha(tmp_path) is None


class TestFromHerokuOrGit:
    def test_uses_heroku_slug_commit_when_set(self, tmp_path):
        os.environ["HEROKU_SLUG_COMMIT"] = "slugabc"
        try:
            bc = from_heroku_or_git(tmp_path)
            assert bc.expected_sha == "slugabc"
        finally:
            del os.environ["HEROKU_SLUG_COMMIT"]

    def test_falls_back_to_git_when_slug_unset(self, tmp_path):
        os.environ.pop("HEROKU_SLUG_COMMIT", None)
        # Initialize a real git repo so git rev-parse succeeds.
        subprocess.check_call(["git", "init", "--quiet", str(tmp_path)])
        subprocess.check_call(["git", "-C", str(tmp_path), "config", "user.email", "t@t.com"])
        subprocess.check_call(["git", "-C", str(tmp_path), "config", "user.name", "T"])
        (tmp_path / "f.txt").write_text("x")
        subprocess.check_call(["git", "-C", str(tmp_path), "add", "f.txt"])
        subprocess.check_call(["git", "-C", str(tmp_path), "commit", "-m", "i", "--quiet"])
        expected = subprocess.check_output(["git", "-C", str(tmp_path), "rev-parse", "HEAD"], text=True).strip()
        bc = from_heroku_or_git(tmp_path)
        assert bc.expected_sha == expected

    def test_returns_empty_sha_when_both_fail(self, tmp_path):
        os.environ.pop("HEROKU_SLUG_COMMIT", None)
        # No git repo — git rev-parse fails. Returns empty SHA.
        bc = from_heroku_or_git(tmp_path)
        assert bc.expected_sha == ""

    def test_treats_empty_slug_env_as_unset(self, tmp_path):
        os.environ["HEROKU_SLUG_COMMIT"] = "   "
        try:
            bc = from_heroku_or_git(tmp_path)
            # Empty-after-strip is treated as "not set" — falls through.
            assert bc.expected_sha in ("", "")  # either empty or from git
        finally:
            del os.environ["HEROKU_SLUG_COMMIT"]


class TestShortSha:
    def test_truncates_to_first_seven(self):
        assert short_sha("b5e191c5df481d51c4e7d1cced51cf7c656f1ead") == "b5e191c"

    def test_passes_through_short_input(self):
        assert short_sha("abc1234") == "abc1234"

    def test_handles_exact_length(self):
        """A string exactly equal to SHORT_SHA_LEN should be passed through,
        not truncated to zero or otherwise mangled."""
        assert SHORT_SHA_LEN == 7
        assert short_sha("abc1234") == "abc1234"
        assert len(short_sha("abc1234")) == 7

    def test_handles_empty_string(self):
        """Empty input is a degenerate case (a SHA-less ref) — passed through
        rather than exploded, so callers can log it without crashing."""
        assert short_sha("") == ""
