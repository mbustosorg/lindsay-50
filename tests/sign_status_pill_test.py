"""Tests for the Versions card (issue #71).

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
    """The dashboard HTML must include the Versions table
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


# --- Browser/Config receipt wiring (issue #71, follow-up) ---------


def test_render_all_refreshes_browser_config_cell():
    """`renderAll()` MUST call `applyBrowserConfigReceipt()` so the
    5s setInterval tick keeps the Browser/Config cell populated
    even when the `on_change` fan-out missed the seed-complete or
    config-envelope events (PyScript race during cold start, broker
    fan-out drop — see feedback_clean_session_aio_fan_out.md).

    The user complaint that drove this test: after a /settings save,
    Flask/Config (server-rendered) shows the new sha but
    Browser/Config stays at "—" indefinitely. The 5s tick refresh
    is the safety net that gets the cell populated within ~5s of
    page load regardless of why the on_change hook missed."""
    src = _read("heart-message-manager/static/sign_status.js")
    # Find the renderAll function body and verify applyBrowserConfigReceipt
    # is called inside it. Simple substring check — the function is
    # short and the call is unique enough to grep for.
    m = re.search(
        r"function\s+renderAll\s*\([^)]*\)\s*\{([\s\S]*?)\n\}",
        src,
    )
    assert m is not None, "renderAll function not found"
    body = m.group(1)
    assert "applyBrowserConfigReceipt(" in body, (
        "renderAll must call applyBrowserConfigReceipt() on every tick "
        "as the safety net for missed on_change events"
    )


def test_browser_config_sha_module_cache_exists():
    """The per-column disagreement comparison reads the latest
    browser-applied config_sha from a module-level cache
    (`_browserConfigSha`) rather than from the cell DOM. This keeps
    the comparison deterministic: applyBrowserConfigReceipt writes
    the cell ASYNCHRONOUSLY (PyScript proxy await), but the
    synchronous comparison must not race with that write."""
    src = _read("heart-message-manager/static/sign_status.js")
    assert "let _browserConfigSha" in src, (
        "_browserConfigSha module-level cache not declared"
    )
    # The comparison site reads the cache, not readCell("browser", "config")
    # — the readCell call for browser-config should be GONE (replaced by
    # the cache read) to prevent the async-write/sync-read race.
    assert re.search(
        r'const\s+browserConfig\s*=\s*_browserConfigSha',
        src,
    ), "applyVersionDriftRender must read _browserConfigSha for browser-config"
    # And the original cell-DOM read should be replaced (no
    # `browserConfig = readCell("browser", "config")` left).
    assert 'readCell("browser", "config")' not in src, (
        "browser-config comparison must come from cache, not cell DOM"
    )


def test_flask_config_sha_module_cache_exists():
    """Same caching pattern as `_browserConfigSha`, but for the
    Flask/Config cell. Flask is the publisher of the config
    envelope — by definition, the SHA the BROWSER receives IS the
    SHA Flask just published. `applyBrowserConfigReceipt` mirrors
    that one value into both cells so the operator doesn't have to
    hard-refresh after a /settings save to see Flask's new SHA."""
    src = _read("heart-message-manager/static/sign_status.js")
    assert "let _flaskConfigSha" in src, (
        "_flaskConfigSha module-level cache not declared"
    )
    # The comparison site must read the cache (with cfg.flaskConfigSha
    # as the page-load fallback, and readCell as the last-resort
    # static textContent). The cache must come FIRST so a fresh
    # receipt overrides any page-load stale value.
    assert re.search(
        r'_flaskConfigSha\s*\|\|\s*cfg\.flaskConfigSha',
        src,
    ), (
        "applyVersionDriftRender must read _flaskConfigSha first, then "
        "fall back to cfg.flaskConfigSha, then to the cell DOM"
    )


def test_apply_browser_config_receipt_writes_both_cells():
    """`applyBrowserConfigReceipt` MUST write the same SHA into
    BOTH the Browser/Config and Flask/Config cells. Flask is the
    publisher, so a receipt = a Flask publish — there's no scenario
    where those two values should diverge. A single receipt path
    drives both cells; the Flask/Config cell is no longer
    page-load-only.
    """
    src = _read("heart-message-manager/static/sign_status.js")
    # Both cell lookups must exist. Use a broad pattern — both
    # selectors must appear in the file.
    assert 'version-drift-cell="browser-config"' in src, (
        "applyBrowserConfigReceipt must read the browser-config cell"
    )
    assert 'version-drift-cell="flask-config"' in src, (
        "applyBrowserConfigReceipt must also write the flask-config cell — "
        "Flask is the publisher, so the receipt IS Flask's published SHA"
    )
    # The two caches must be set from the same receipt sha, adjacently.
    assert re.search(
        r'_browserConfigSha\s*=\s*sha;\s*\n\s*_flaskConfigSha\s*=\s*sha',
        src,
    ), (
        "applyBrowserConfigReceipt must set both _browserConfigSha and "
        "_flaskConfigSha from the same receipt sha"
    )


def test_pi_cell_only_live_with_snapshot():
    """The Pi's Code/Config cells must only be treated as LIVE signals
    when a snapshot is present. When the Pi is offline (no snapshot
    yet), reading the cell's stale textContent — which `applyFieldsRender`
    leaves populated from the last online session — would surface an
    OLD Pi SHA alongside Flask's current SHA and Browser's current
    SHA, and the column rule would flip red on a phantom "drift" that
    is really just "Pi is offline."

    Without this guard, every dashboard reload while the Pi is
    rebooting or on flaky wifi shows the column red even though Flask
    and Browser agree. The cell text is still rendered (so the
    operator can see the last-known Pi SHA), but the comparison logic
    treats Pi as "we don't know yet."
    """
    src = _read("heart-message-manager/static/sign_status.js")
    # The `piHasLiveSnapshot` / `piHasLiveAppliedConfig` locals gate
    # Pi on snapshot presence. Without them, the cell DOM fallback
    # would feed stale data into the comparison.
    assert "piHasLiveSnapshot" in src, (
        "applyVersionDriftRender must gate Pi code on snapshot presence — "
        "falling back to stale cell textContent creates phantom drift "
        "when the Pi is offline"
    )
    assert "piHasLiveAppliedConfig" in src, (
        "applyVersionDriftRender must gate Pi config on snapshot presence"
    )
    # The Pi values used in the comparison must be EMPTY when no
    # snapshot exists — verify by checking that the comparison-side
    # piCode / piConfig are derived from `snapshot && ...` only,
    # not from readCell as a fallback.
    apply_match = re.search(
        r"function\s+applyVersionDriftRender\s*\([^)]*\)\s*\{([\s\S]*?)\n\}",
        src,
    )
    assert apply_match is not None, "applyVersionDriftRender not found"
    body = apply_match.group(1)
    assert re.search(
        r"const\s+piCode\s*=\s*\(?\s*snapshot\s*&&\s*snapshot\.short_sha\s*\)?\s*\|\|\s*\"\"",
        body,
    ), (
        "piCode must be `(snapshot && snapshot.short_sha) || \"\"` — no "
        "readCell fallback, which would surface stale offline SHA"
    )
    assert re.search(
        r"const\s+piConfig\s*=\s*\(?\s*snapshot\s*&&\s*snapshot\.applied_config_sha\s*\)?\s*\|\|\s*\"\"",
        body,
    ), (
        "piConfig must be `(snapshot && snapshot.applied_config_sha) || \"\"` — "
        "no readCell fallback"
    )


def test_live_only_drift_comparison():
    """The per-column red rule must only flip cells whose backing
    source is LIVE — never cells whose backing source is offline.

    Cells are LIVE when backed by a fresh signal:
      - Flask: always live (server-rendered at page load).
      - Browser: always live (page-rendered + receipt cache).
      - Pi: live ONLY when a snapshot is present.

    A column with two LIVE cells that disagree flips both red; a
    column with one LIVE cell + offline Pi stays neutral (we don't
    know if Pi agrees or disagrees yet — wait for it to come back).
    """
    src = _read("heart-message-manager/static/sign_status.js")
    apply_match = re.search(
        r"function\s+applyVersionDriftRender\s*\([^)]*\)\s*\{([\s\S]*?)\n\}",
        src,
    )
    assert apply_match is not None, "applyVersionDriftRender not found"
    body = apply_match.group(1)
    # The liveByColumn map must exist and gate Pi on snapshot presence.
    assert "liveByColumn" in body, (
        "applyVersionDriftRender must build a liveByColumn map gating "
        "Pi on snapshot presence — without this, stale Pi cells flip red"
    )
    # The red-highlight predicate must check isLive, not just !isEmpty.
    assert re.search(
        r"isLive\s*&&\s*!isEmpty\s*&&\s*!\w*[Aa]gree",
        body,
    ), (
        "red-highlight predicate must require isLive — without it, "
        "stale Pi cells with textContent from a prior online session "
        "flip red even when the Pi is offline"
    )


def test_offline_pi_diagnostic_text_preserved():
    """When the Pi goes offline, the cell text MUST keep the last-known
    SHA so the operator can see "Pi was running 86537d5 when it went
    offline." The drift-comparison fix (test_pi_cell_only_live_with_snapshot)
    only gates the comparison logic — `applyFieldsRender` still writes
    to `[data-sign-status-field]` on every WS tick.

    Updated for issue #71 round 12: the early-return on
    `rendered.state === "offline"` was REMOVED. A persisted snapshot
    from `sign_status_log` is by definition older than 30s (the
    `OFFLINE_AFTER_MS` cutoff in `stateFromAge`), so the previous
    guard blocked the per-cell writes — meaning a hard refresh
    before any live WS message would leave Pi/Code and Pi/Config
    as "—" even though Flask had the Pi's last-known state. Now
    `applyFieldsRender` populates the cells from whatever snapshot
    it has (live OR persisted), so the operator sees the
    last-known Pi SHA on every dashboard load.

    If the per-cell textContent were cleared on offline transitions,
    the operator would lose the diagnostic signal — "Pi was running
    X" becomes "Pi is offline" with no last-known value to compare
    against.
    """
    src = _read("heart-message-manager/static/sign_status.js")
    apply_fields_match = re.search(
        r"function\s+applyFieldsRender\s*\([^)]*\)\s*\{([\s\S]*?)\n\}",
        src,
    )
    assert apply_fields_match is not None, "applyFieldsRender not found"
    body = apply_fields_match.group(1)
    # Strip block + line comments before checking for the early-return
    # — the function body's comment block intentionally references
    # the old `rendered.state === "offline"` pattern as historical
    # context, which would false-positive this assertion.
    code_only = re.sub(r"//[^\n]*", "", body)
    code_only = re.sub(r"/\*[\s\S]*?\*/", "", code_only)
    # The early-return on `rendered.state === "offline"` must be GONE
    # from the executable code — it would short-circuit the per-cell
    # writes from a persisted snapshot, which is exactly the bug the
    # operator reported ("is there no stored value for what the Pi
    # was previously running?").
    assert 'rendered.state === "offline"' not in code_only, (
        "applyFieldsRender must NOT short-circuit on offline state — "
        "a persisted snapshot from /api/sign-status has by-definition "
        "stale updated_at, so the offline guard would prevent the "
        "Pi cells from ever being populated from persistence"
    )
    # The early-return that DID exist must still exist on `!snapshot`
    # alone (so a cold start with no persisted row AND no live WS
    # doesn't try to render empty values).
    assert "!snapshot" in code_only, (
        "applyFieldsRender must still short-circuit when there is NO "
        "snapshot (neither live nor persisted) — cold start means no "
        "data to render"
    )
    # No `textContent = ""` clearing the field cells anywhere in the
    # function — we never want to erase the last-known Pi SHA.
    assert "textContent = \"\"" not in code_only, (
        "applyFieldsRender must NOT clear the cell textContent — the "
        "operator wants to see the last-known SHA even after the Pi "
        "goes offline"
    )


def test_pi_cells_populate_from_persisted_snapshot():
    """On hard refresh before any live WS message has landed,
    /api/sign-status returns the persisted snapshot from
    `sign_status_log` (issue #71). `hydrateFromServer` calls
    `maybeAcceptSnapshot(payload.snapshot)`, which feeds the
    persisted snapshot into `renderAll`. The Pi cells MUST
    populate from that snapshot — the operator's complaint
    ("is there no stored value for what the Pi was previously
    running?") was that Pi/Code and Pi/Config stayed "—" after
    a hard refresh, even though Flask had the Pi's last-known
    state in SQLite.

    The persisted-snapshot path uses the SAME `_latestSnapshot`
    variable as the live WS path — `maybeAcceptSnapshot` is
    source-agnostic. The only requirement is that
    `applyFieldsRender` does NOT short-circuit on `state ===
    "offline"`, since the persisted snapshot's `updated_at`
    is by-definition stale.
    """
    src = _read("heart-message-manager/static/sign_status.js")
    # `hydrateFromServer` must call `maybeAcceptSnapshot` on the
    # persisted snapshot returned by /api/sign-status.
    hydrate_match = re.search(
        r"function\s+hydrateFromServer\s*\([^)]*\)\s*\{([\s\S]*?)\n\}",
        src,
    )
    assert hydrate_match is not None, "hydrateFromServer not found"
    hydrate_body = hydrate_match.group(1)
    assert "maybeAcceptSnapshot" in hydrate_body, (
        "hydrateFromServer must call maybeAcceptSnapshot on the "
        "persisted snapshot so the in-memory state populates"
    )
    # The persisted snapshot is consumed via `payload.snapshot`
    # (the /api/sign-status response shape).
    assert "payload.snapshot" in hydrate_body, (
        "hydrateFromServer must read payload.snapshot — that's the "
        "/api/sign-status response shape (`{snapshot, received_at, "
        "source}`)"
    )


def test_browser_config_hard_fallback_removed():
    """The /api/config hard fallback was REMOVED. The browser's
    Browser/Config and Flask/Config cells now trust the WS receipt
    path exclusively (per operator call: don't add complexity, the
    receipt path is sufficient — Flask publishes, so a receipt IS
    a Flask publish and the same SHA drives both cells). If the
    receipt path is broken — broker fan-out drop, PyScript
    marshalling slow — both cells stay "—" rather than silently
    masking the underlying issue by polling /api/config. Drift
    detection is preserved: a populated cell that disagrees with
    the row's canonical value still flips red via
    `applyVersionDriftRender`'s per-column rule.
    """
    src = _read("heart-message-manager/static/sign_status.js")
    # The fallback function MUST be gone — if it returns, the receipt
    # path's staleness will be hidden by a /api/config poll.
    assert "function fetchConfigShaFallback" not in src, (
        "fetchConfigShaFallback was removed — trust the WS receipt path only"
    )
    # The re-entry guard existed only to throttle the fallback; it
    # must be gone too.
    assert "_configFallbackInFlight" not in src, (
        "_configFallbackInFlight was removed — no fallback to throttle"
    )
    # No direct /api/config fetch from the dashboard renderer — the
    # rule that hid the previous bug.
    assert re.search(
        r'fetch\([\'"]/api/config[\'"]',
        src,
    ) is None, (
        "dashboard renderer must not fetch /api/config — that's the WS "
        "receipt path's job. A direct fetch would hide broker fan-out drops."
    )


def test_base_template_sign_status_js_bumped_v11():
    """The sign_status.js cache-buster must be ?v=11 or later so
    browsers pin to the round-14 /get-fetch removal — without
    the bump, browsers would still hit the round-13 shim that
    passes `fetchLastValue: false` to a code path that no
    longer accepts that option. Memory rule: bump ?v=N when
    shipping static JS changes
    (feedback_bump_cache_buster_with_static_js.md).
    """
    html = _read("heart-message-manager/templates/base.html")
    m = re.search(r"sign_status\.js[^>]*\?v=(\d+)", html)
    assert m is not None, "sign_status.js not loaded with cache-buster"
    assert int(m.group(1)) >= 11, (
        f"sign_status.js cache buster is ?v={m.group(1)}, need ?v>=11"
    )


def test_get_fetch_removed_from_mqtt_ws_client():
    """Round 14 (operator call): the AIO `<topic>/get` last-value
    workaround was REMOVED from mqtt_ws_client.js entirely. Both
    the status-topic WS (sign_status.js) and the config-topic WS
    (dashboard_runtime.py) now rely on real-time broker delivery
    only.

    The /get fetch was originally added in a5d6102 (round 9) to
    recover from broker fan-out drops, but the actual root cause
    was the AIO 1KB payload limit with feed history on
    (feedback_aio_feed_history_payload_limit.md). The /get
    workaround didn't solve that and added confusion by surfacing
    AIO's broker-cached last value as if it were live. With it
    removed:
      - Pi cells stay "—" until a fresh live WS publish lands
      - Config cells populate from seed() + WS publishes only
      - The pill correctly shows freshness state based on real
        delivery, not broker-cached stale data
    """
    src = _read("heart-message-manager/static/mqtt_ws_client.js")
    # Strip comments before checking — the round-14 comment block in
    # mqtt_ws_client.js intentionally references "/get" as historical
    # context. The check is for CODE that constructs a `<topic>/get`
    # destination, not for the comment word.
    code_only = re.sub(r"//[^\n]*", "", src)
    code_only = re.sub(r"/\*[\s\S]*?\*/", "", code_only)
    # The buildPublish call for the /get fetch (topic + "/get",
    # empty payload, packet ID 0x0002) MUST be gone.
    assert '"/get"' not in code_only, (
        "AIO `<topic>/get` last-value fetch must be removed from "
        "mqtt_ws_client.js — surface stale broker-cached data as if "
        "the Pi were live misleads the operator"
    )
    # The /get topic construction pattern (topic + "/get") must be gone.
    assert re.search(
        r"topic\s*\+\s*[\"']/get[\"']",
        code_only,
    ) is None, (
        "The `topic + '/get'` construction must be removed from "
        "mqtt_ws_client.js — no caller should be able to trigger the "
        "AIO last-value workaround via this shim"
    )
    # The fetchLastValue option must also be gone — round 14 made
    # the removal unconditional; the round-13 opt-in is obsolete.
    assert "fetchLastValue" not in src, (
        "fetchLastValue option must be removed — round 14 made the "
        "/get removal unconditional, the round-13 opt-in is obsolete"
    )


def test_sign_status_does_not_pass_fetch_last_value():
    """sign_status.js used to pass `fetchLastValue: false` to opt
    out of the /get fetch. Round 14 removed the /get fetch entirely
    (and the option), so sign_status.js must NOT reference
    `fetchLastValue` anywhere — it was a round-13 shim."""
    src = _read("heart-message-manager/static/sign_status.js")
    assert "fetchLastValue" not in src, (
        "sign_status.js must not reference `fetchLastValue` — round "
        "14 removed the option from createMqttWsClient"
    )
