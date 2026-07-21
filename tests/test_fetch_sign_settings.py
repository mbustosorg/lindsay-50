"""Tests for lib_shared.boot_config.fetch_sign_settings (issue #51).

The Pi's loader calls `fetch_sign_settings` on boot to learn the
operator-pinned target version (or Flask's self-SHA when no pin
is set). The function mirrors `fetch_boot_config`'s shape and
failure policy: returns the resolved `target_version` short SHA
on success, None on any failure (network, timeout, non-200,
malformed JSON, missing/empty `target_version`).

The endpoint guarantees a concrete short SHA on the wire — a None
or empty response means either Flask is broken or the response is
stale. The loader treats that as "fall through to current/.../main.py"
(safe default).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from lib_shared import boot_config


def _mock_response(status_code=200, json_payload=None, raise_json=False):
    """Build a `requests`-like response mock."""
    resp = MagicMock()
    resp.status_code = status_code
    if raise_json:
        resp.json.side_effect = ValueError("bad json")
    else:
        resp.json.return_value = json_payload
    return resp


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestFetchSignSettingsSuccess:
    def test_returns_target_version_on_200(self):
        requests_module = MagicMock()
        requests_module.get.return_value = _mock_response(200, {"target_version": "abc1234", "timezone": "US/Pacific"})
        result = boot_config.fetch_sign_settings(
            api_url="https://example.com/api/messages",
            api_key="secret",
            requests_module=requests_module,
        )
        assert result == "abc1234"

    def test_sends_x_api_key_header(self):
        requests_module = MagicMock()
        requests_module.get.return_value = _mock_response(200, {"target_version": "abc1234"})
        boot_config.fetch_sign_settings(
            api_url="https://example.com/api/messages",
            api_key="secret-key",
            requests_module=requests_module,
        )
        _, kwargs = requests_module.get.call_args
        assert kwargs["headers"]["X-API-Key"] == "secret-key"

    def test_calls_sign_settings_path(self):
        """Endpoint URL is the SIGN_SETTINGS_PATH constant, derived
        from the api_url origin."""
        requests_module = MagicMock()
        requests_module.get.return_value = _mock_response(200, {"target_version": "abc1234"})
        boot_config.fetch_sign_settings(
            api_url="https://example.com/api/messages",
            api_key="k",
            requests_module=requests_module,
        )
        called_url = requests_module.get.call_args.args[0]
        assert called_url == "https://example.com/api/sign/settings"

    def test_resolves_path_relative_to_api_url_origin(self):
        """api_url path is stripped — only the origin matters."""
        requests_module = MagicMock()
        requests_module.get.return_value = _mock_response(200, {"target_version": "abc1234"})
        boot_config.fetch_sign_settings(
            api_url="https://example.com/some/other/path",
            api_key="k",
            requests_module=requests_module,
        )
        called_url = requests_module.get.call_args.args[0]
        assert called_url == "https://example.com/api/sign/settings"

    def test_forwards_timeout(self):
        requests_module = MagicMock()
        requests_module.get.return_value = _mock_response(200, {"target_version": "abc1234"})
        boot_config.fetch_sign_settings(
            api_url="https://example.com/api/messages",
            api_key="k",
            timeout=2.5,
            requests_module=requests_module,
        )
        _, kwargs = requests_module.get.call_args
        assert kwargs["timeout"] == 2.5


# ---------------------------------------------------------------------------
# Failure modes — every path returns None
# ---------------------------------------------------------------------------


class TestFetchSignSettingsFailures:
    def test_missing_api_url_returns_none(self):
        requests_module = MagicMock()
        result = boot_config.fetch_sign_settings(
            api_url="",
            api_key="k",
            requests_module=requests_module,
        )
        assert result is None
        # Must NOT call out — empty URL is a configuration bug, not a network one.
        requests_module.get.assert_not_called()

    def test_missing_api_key_returns_none(self):
        requests_module = MagicMock()
        result = boot_config.fetch_sign_settings(
            api_url="https://example.com/api/messages",
            api_key="",
            requests_module=requests_module,
        )
        assert result is None
        requests_module.get.assert_not_called()

    def test_invalid_api_url_returns_none(self):
        requests_module = MagicMock()
        # No scheme/netloc → urlparse is unhappy.
        result = boot_config.fetch_sign_settings(
            api_url="not-a-url",
            api_key="k",
            requests_module=requests_module,
        )
        assert result is None
        requests_module.get.assert_not_called()

    def test_network_exception_returns_none(self):
        requests_module = MagicMock()
        requests_module.get.side_effect = ConnectionError("dns down")
        result = boot_config.fetch_sign_settings(
            api_url="https://example.com/api/messages",
            api_key="k",
            requests_module=requests_module,
        )
        assert result is None

    def test_timeout_returns_none(self):
        requests_module = MagicMock()
        requests_module.get.side_effect = TimeoutError("timed out")
        result = boot_config.fetch_sign_settings(
            api_url="https://example.com/api/messages",
            api_key="k",
            requests_module=requests_module,
        )
        assert result is None

    @pytest.mark.parametrize("code", [400, 401, 403, 500, 502, 503])
    def test_non_200_returns_none(self, code):
        requests_module = MagicMock()
        requests_module.get.return_value = _mock_response(code, {"target_version": "abc1234"})
        result = boot_config.fetch_sign_settings(
            api_url="https://example.com/api/messages",
            api_key="k",
            requests_module=requests_module,
        )
        assert result is None

    def test_malformed_json_returns_none(self):
        requests_module = MagicMock()
        requests_module.get.return_value = _mock_response(200, None, raise_json=True)
        result = boot_config.fetch_sign_settings(
            api_url="https://example.com/api/messages",
            api_key="k",
            requests_module=requests_module,
        )
        assert result is None

    def test_non_dict_payload_returns_none(self):
        requests_module = MagicMock()
        requests_module.get.return_value = _mock_response(200, ["abc1234"])
        result = boot_config.fetch_sign_settings(
            api_url="https://example.com/api/messages",
            api_key="k",
            requests_module=requests_module,
        )
        assert result is None

    def test_missing_target_version_returns_none(self):
        requests_module = MagicMock()
        requests_module.get.return_value = _mock_response(200, {"timezone": "US/Pacific"})
        result = boot_config.fetch_sign_settings(
            api_url="https://example.com/api/messages",
            api_key="k",
            requests_module=requests_module,
        )
        assert result is None

    def test_empty_target_version_returns_none(self):
        requests_module = MagicMock()
        requests_module.get.return_value = _mock_response(200, {"target_version": "", "timezone": "US/Pacific"})
        result = boot_config.fetch_sign_settings(
            api_url="https://example.com/api/messages",
            api_key="k",
            requests_module=requests_module,
        )
        assert result is None

    def test_non_string_target_version_returns_none(self):
        requests_module = MagicMock()
        requests_module.get.return_value = _mock_response(200, {"target_version": 12345, "timezone": "US/Pacific"})
        result = boot_config.fetch_sign_settings(
            api_url="https://example.com/api/messages",
            api_key="k",
            requests_module=requests_module,
        )
        assert result is None


# ---------------------------------------------------------------------------
# from_sign_settings_response — unit-level parser tests
# ---------------------------------------------------------------------------


class TestFromSignSettingsResponse:
    def test_valid_payload_returns_target_version(self):
        assert boot_config.from_sign_settings_response({"target_version": "abc1234"}) == "abc1234"

    def test_extra_fields_ignored(self):
        assert (
            boot_config.from_sign_settings_response(
                {"target_version": "abc1234", "timezone": "US/Pacific", "extra": "x"}
            )
            == "abc1234"
        )

    @pytest.mark.parametrize("payload", [None, [], "abc1234", 12345])
    def test_non_dict_returns_none(self, payload):
        assert boot_config.from_sign_settings_response(payload) is None

    @pytest.mark.parametrize("target", [None, "", 12345, [], {}])
    def test_invalid_target_version_returns_none(self, target):
        assert boot_config.from_sign_settings_response({"target_version": target}) is None

    def test_missing_target_version_key_returns_none(self):
        assert boot_config.from_sign_settings_response({"timezone": "US/Pacific"}) is None


# ---------------------------------------------------------------------------
# Endpoint path constant
# ---------------------------------------------------------------------------


class TestSignSettingsPathConstant:
    def test_sign_settings_path_constant(self):
        assert boot_config.SIGN_SETTINGS_PATH == "/api/sign/settings"

    def test_distinct_from_boot_config_path(self):
        assert boot_config.SIGN_SETTINGS_PATH != boot_config.BOOT_CONFIG_PATH
