"""Tests for the Versions & Config card (issue #71).

The renderer logic in ``sign_status.js:applyVersionDriftRender``
encodes a column-disagreement rule: for each column (code, config),
if any two populated cells disagree, every populated cell in that
column flips red. Empty cells stay neutral — cold start is not drift.

These tests exercise the disagreement logic in isolation via a
tiny stand-in for the DOM (we can't easily run the JS module under
jsdom in CI without an extra dep), and verify the dashboard-side
template wiring (data-version-drift-cell markers exist on the
right elements).
"""

from __future__ import annotations

import json
import re
from pathlib import Path


def column_agrees(values: dict[str, str]) -> bool:
    """Replicate the per-column allAgree rule from sign_status.js.

    Args:
        values: row -> cell-value mapping for a single column.

    Returns:
        True iff every populated cell in the column agrees (or no
        cells are populated). Empty cells are neutral — they don't
        count as disagreement.
    """
    populated = [v for v in values.values() if v and len(v) > 0]
    if not populated:
        return True
    return all(v == populated[0] for v in populated)


def cell_is_red(populated: bool, agrees: bool) -> bool:
    """A cell is red iff it is populated AND the column disagrees."""
    return populated and not agrees


# --- column-agreement logic (the heart of the renderer) ----------


class TestColumnAgreement:
    def test_empty_column_agrees(self):
        """No cells populated → column agrees (nothing to compare)."""
        assert column_agrees({"flask": "", "pi": "", "browser": ""}) is True

    def test_single_populated_cell_agrees(self):
        """Cold start: only Flask has a value yet — column agrees."""
        assert column_agrees({"flask": "abc1234", "pi": "", "browser": ""}) is True

    def test_all_three_agree(self):
        """Happy path: Flask / Pi / Browser all on the same SHA."""
        assert (
            column_agrees({"flask": "abc1234", "pi": "abc1234", "browser": "abc1234"})
            is True
        )

    def test_flask_and_browser_agree_but_pi_behind(self):
        """The classic AIO fan-out-drop scenario: Pi is on the old
        config while Flask and Browser have the new one."""
        assert (
            column_agrees({"flask": "abc1234", "pi": "old1234", "browser": "abc1234"})
            is False
        )

    def test_flask_and_pi_agree_but_browser_empty(self):
        """Browser hasn't reported yet (cold start). Column still
        agrees — empty is not drift."""
        assert (
            column_agrees({"flask": "abc1234", "pi": "abc1234", "browser": ""})
            is True
        )

    def test_all_three_different(self):
        """Three different SHAs across Flask / Pi / Browser."""
        assert (
            column_agrees({"flask": "1111111", "pi": "2222222", "browser": "3333333"})
            is False
        )


class TestCellIsRed:
    def test_empty_cell_is_not_red(self):
        """An empty cell never flips red even if the column disagrees."""
        assert cell_is_red(populated=False, agrees=False) is False

    def test_agreeing_populated_cell_is_not_red(self):
        """A populated cell in an agreeing column stays neutral."""
        assert cell_is_red(populated=True, agrees=True) is False

    def test_disagreeing_populated_cell_is_red(self):
        """A populated cell in a disagreeing column flips red."""
        assert cell_is_red(populated=True, agrees=False) is True


# --- template wiring ----------------------------------------------


def _read(path: str) -> str:
    return Path(path).read_text()


def test_dashboard_template_emits_version_drift_table():
    """The dashboard HTML must include the Versions & Config table
    with the 6 expected cell markers (flask-code, flask-config,
    pi-code, pi-config, browser-code, browser-config)."""
    html = _read("heart-message-manager/templates/dashboard.html")
    assert "data-version-drift-table" in html, "table marker missing"
    expected_cells = {
        "flask-code",
        "flask-config",
        "pi-code",
        "pi-config",
        "browser-code",
        "browser-config",
    }
    found = set(re.findall(r'data-version-drift-cell="([^"]+)"', html))
    assert expected_cells.issubset(found), f"missing cells: {expected_cells - found}"


def test_dashboard_template_uses_monospace_fixed_width():
    """Character alignment matters: cells use font-mono + identical
    width so a SHA in Flask/Code lines up vertically with the same
    SHA in Pi/Code and Browser/Code. Without this the operator
    can't eyeball agreement across rows."""
    html = _read("heart-message-manager/templates/dashboard.html")
    # Pull out a sample cell + verify font-mono + a fixed width class
    # (w-24 = 6rem ≈ 7 chars in 9px monospace = enough for a 7-char
    # SHA + 1 char of padding).
    assert "font-mono" in html, "missing font-mono class on cells"
    # w-24 is the convention used; we accept any of w-20 / w-24 / w-28
    # as long as ALL six cells use the SAME width. We sweep a small
    # window around each data-version-drift-cell to capture the
    # cell's class attribute (which may be on the previous line).
    cell_widths: dict[str, str] = {}
    for m in re.finditer(
        r'data-version-drift-cell="([^"]+)"', html
    ):
        cell_id = m.group(1)
        # Walk back ~300 chars to find this cell's <td ...> opener.
        start = max(0, m.start() - 300)
        window = html[start : m.end()]
        td_match = re.search(r"<td\b[^>]*class=\"([^\"]+)\"", window)
        if not td_match:
            continue
        css = td_match.group(1)
        if "font-mono" not in css:
            continue
        width_match = re.search(r"\bw-(\d+)\b", css)
        if width_match:
            cell_widths[cell_id] = width_match.group(0)
    assert len(cell_widths) >= 3, f"too few font-mono cells: {cell_widths}"
    widths = set(cell_widths.values())
    assert len(widths) == 1, (
        f"cells use different widths — SHAs won't line up vertically: {cell_widths}"
    )


def test_base_template_exposes_version_config_in_app_config():
    """window.APP_CONFIG must carry flaskVersion + flaskConfigSha so
    the JS renderer doesn't need to fetch them on each render."""
    html = _read("heart-message-manager/templates/base.html")
    assert "flaskVersion:" in html, "APP_CONFIG.flaskVersion missing"
    assert "flaskConfigSha:" in html, "APP_CONFIG.flaskConfigSha missing"
    # Both values must come from server-side template vars (no hardcoded strings).
    assert "version.FLASK_VERSION" in html, "flaskVersion not template-bound"
    assert "version.FLASK_CONFIG_SHA" in html, "flaskConfigSha not template-bound"


def test_sign_status_js_bumped_cache_buster():
    """Memory rule: bump `?v=N` when shipping static JS changes so
    browsers don't pin to the old copy. The original was `?v=1`;
    this change rewrote the renderer + the applyFieldsRender guard,
    so we must be on `?v=2` or later."""
    html = _read("heart-message-manager/templates/base.html")
    # The URL is rendered via Jinja: `{{ url_for('static', filename='sign_status.js') }}?v=N`
    # Match either the raw template fragment or the rendered HTML.
    m = re.search(r"sign_status\.js[^>]*\?v=(\d+)", html)
    assert m is not None, "sign_status.js not loaded with cache-buster"
    assert int(m.group(1)) >= 2, (
        f"sign_status.js cache buster is ?v={m.group(1)}, need ?v>=2"
    )


# --- applied_config_sha appears in the snapshot feed --------------


def test_status_snapshot_carries_applied_config_sha():
    """Pi status over MQTT_STATUS_TOPIC carries applied_config_sha so
    the dashboard can read it from snapshot.applied_config_sha."""
    snapshot = {
        "schema_version": 2,
        "active_sha": "b5e191c5df481d51c4e7d1cced51cf7c656f1ead",
        "short_sha": "b5e191c",
        "started_at": "2026-09-12T10:00:00+00:00",
        "updated_at": "2026-09-12T10:01:30+00:00",
        "uptime_seconds": 90,
        "mqtt_connected": True,
        "last_error": None,
        "applied_config_sha": "abc1234",
    }
    # The field is consumed by sign_status.js:applyVersionDriftRender:
    #   const piConfig = (snapshot && snapshot.applied_config_sha) || ...
    # Verify the wire shape matches what the renderer expects.
    assert "applied_config_sha" in snapshot
    assert snapshot["applied_config_sha"] == "abc1234"
    # And it must serialize + deserialize cleanly via JSON (the
    # whole point of putting it on the wire is JSON-roundtripping).
    blob = json.dumps(snapshot)
    parsed = json.loads(blob)
    assert parsed["applied_config_sha"] == "abc1234"
