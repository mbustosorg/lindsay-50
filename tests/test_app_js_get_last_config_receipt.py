"""Regression tests for App.getLastConfigReceipt (issue #71, rounds 8 / 12 / 13).

Symptom history:

- v208 (round 12): Preview/Config cell stayed "—" even though
  Python-side `_last_applied_config_sha` was set to the correct SHA
  (`c77bcde`). Flask/Config populated correctly because that's
  server-rendered from `{{ flask_config_sha }}`.
  Root cause: `App.getLastConfigReceipt()` did
  `await window._message_manager.get_last_config_receipt()`.
  PyScript proxies return the proxy itself when awaited, NOT the
  unwrapped Python value, so `receipt.sha` was undefined → cell
  stayed "—".
  Fix (v208): drop the `await`. Read `.sha` directly off the
  returned object.

- v212 (round 13, this test): the v208 fix worked in early PyScript
  builds where the JsProxy-of-dict exposed dict keys as direct JS
  properties. PyScript 2024.9.1 (pinned in dashboard.html) changed
  the JsProxy surface — dict keys are NOT direct properties any
  more. `receipt.sha` is now `undefined`. Operator confirmed via
  JS console: `_last_applied_config_sha` was `'713ad52'` and
  `_config.config_sha` was `'713ad52'`, but `getLastConfigReceipt()`
  returned `{sha: ""}` because `proxy.sha` is undefined.
  Fix (v212): use a multi-strategy unwrap — try
  `Object.fromEntries(Object.entries(receipt))` first, fall back to
  `receipt.get('sha')`, fall back to `receipt.sha` direct access.
  All three strategies coexist because the proxy surface varies
  across Pyodide builds.

These tests pin the source shape: we don't need a live PyScript to
catch the regression — we just check that `app.js` reads the SHA
through the documented unwrap strategies, never through `await` on
the PyScript proxy, and never through a bare `receipt.sha` as the
SOLE path (it must be paired with the proxy-safe fallbacks).
"""

from __future__ import annotations

import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))


def _app_js_body():
    """Return the body of `App.getLastConfigReceipt` from app.js.

    Locates the function by its signature and returns the body up
    to the next top-level blank-line + closing brace (mirrors the
    original test's bounded-read approach).
    """
    src = (_PROJECT_ROOT / "heart-message-manager" / "static" / "app.js").read_text()
    fn_idx = src.find("async function getLastConfigReceipt")
    assert fn_idx != -1, "app.js must define getLastConfigReceipt"
    return src[fn_idx : fn_idx + 3000]


def test_app_js_does_not_await_get_last_config_receipt():
    """`App.getLastConfigReceipt()` must NOT `await` the PyScript proxy.

    PyScript proxies: `await proxy.sync_method()` returns the proxy
    itself, not the underlying Python value. The receipt dict
    `{"sha": str}` has no proxy wrappers to unwrap — `.sha` is
    accessible directly via `proxy.get('sha')` /
    `Object.entries(proxy)` even when direct property access fails.
    Awaiting the proxy would yield the proxy back, and the caller's
    `receipt.sha` would be undefined → Preview/Config cell stays "—".

    Pin: the source for `App.getLastConfigReceipt` must call
    `window._message_manager.get_last_config_receipt()` WITHOUT a
    preceding `await` keyword.
    """
    body = _app_js_body()
    assert "await window._message_manager.get_last_config_receipt()" not in body, (
        "App.getLastConfigReceipt must NOT await the PyScript proxy — "
        "awaiting a sync method on a PyScript proxy returns the proxy "
        "itself, not the underlying dict. Symptom: Preview/Config "
        "cell stayed '—' even though Python-side "
        "_last_applied_config_sha was populated."
    )


def test_app_js_uses_proxy_safe_unwrap():
    """Pin the v212 multi-strategy unwrap.

    The v208 fix relied on `proxy.sha` direct property access. In
    PyScript 2024.9.1 that doesn't work for dict proxies — keys are
    NOT exposed as direct properties. The v212 fix adds three
    unwrap strategies, in priority order:

      1. `Object.fromEntries(Object.entries(receipt))` — works on
         most JsProxy-of-dict.
      2. `receipt.get('sha')` — works on all JsProxy-of-dict
         because PyProxy exposes the dict's `.get()` method.
      3. `receipt.sha` — fallback for plain JS objects.

    If any of these three go missing, the test fails. The test
    doesn't pin exact syntax (so the strategy set can evolve),
    just that all three are present.
    """
    body = _app_js_body()
    # Strategy 1: Object.fromEntries + Object.entries
    assert "Object.fromEntries" in body, (
        "v212 fix must include Object.fromEntries(Object.entries(receipt)) "
        "as the primary unwrap strategy — works on most PyScript 2024.9.x "
        "JsProxy-of-dict."
    )
    assert "Object.entries" in body, (
        "v212 fix must include Object.entries(receipt) — the only way "
        "to enumerate a JsProxy-of-dict's keys in PyScript 2024.9.x."
    )
    # Strategy 2: receipt.get('sha')
    assert 'receipt.get("sha")' in body or "receipt.get('sha')" in body, (
        "v212 fix must include receipt.get('sha') as a fallback — "
        "JsProxy of Python dict always exposes the dict's .get() "
        "method, even when direct property access fails."
    )
    # Strategy 3: direct .sha access as last resort
    assert "receipt.sha" in body, (
        "v212 fix must keep `receipt.sha` direct access as a last-resort "
        "fallback for plain JS objects (the v208 case)."
    )


def test_app_js_unwrap_priority_is_proxy_safe_first():
    """The unwrap priority must put proxy-safe strategies BEFORE
    direct property access. The direct `.sha` access is the v208
    path that BROKE on PyScript 2024.9.x — if it becomes the
    primary (sole) path again, the symptom recurs.

    We pin the ordering loosely by matching on the EXECUTION forms
    (e.g. `sha = unwrapped.sha`, `receipt.get("sha")`,
    `sha = receipt.sha`) — not on prose comments, which can
    mention the rejected path by name without actually invoking it.
    Both proxy-safe execution forms must appear in the source
    BEFORE the bare `sha = receipt.sha` execution form.
    """
    import re

    body = _app_js_body()
    # Match execution forms, not comments / docstrings.
    ofe_idx = body.find("Object.fromEntries(Object.entries(receipt))")
    get_match = re.search(r"receipt\.get\(\s*['\"]sha['\"]\s*\)", body)
    direct_match = re.search(r"sha\s*=\s*receipt\.sha\b", body)

    assert ofe_idx != -1, (
        "Object.fromEntries(Object.entries(receipt)) execution form "
        "must be present."
    )
    assert get_match is not None, (
        "receipt.get('sha') execution form must be present."
    )
    assert direct_match is not None, (
        "`sha = receipt.sha` execution form must be present (it's the "
        "v208 path; we keep it as a last-resort fallback for plain "
        "JS objects)."
    )

    # Proxy-safe strategies must appear in source order BEFORE the
    # unsafe direct-access form. If a future refactor reorders them
    # so the unsafe form comes first, the proxy-safe fallbacks
    # become unreachable in the most common case (PyScript
    # 2024.9.1, the version pinned in dashboard.html).
    assert ofe_idx < direct_match.start(), (
        "Object.fromEntries (proxy-safe) execution form must appear "
        "BEFORE `sha = receipt.sha` (unsafe direct access). If a "
        "future refactor reorders them, the proxy-safe strategy is "
        "no longer reachable and the v208 symptom recurs."
    )
    assert get_match.start() < direct_match.start(), (
        "receipt.get('sha') (proxy-safe via dict method) must "
        "appear BEFORE `sha = receipt.sha` (unsafe direct access). "
        "If a future refactor reorders them, the unsafe path can "
        "become the primary."
    )


def test_app_js_unwrap_logs_failure():
    """The function must log a warning when the unwrap fails (so
    the operator can see why a future regression breaks Preview/Config
    instead of silently returning `{sha: ""}`).

    The v208 fix had `console.warn("getLastConfigReceipt failed:", e)`
    inside a try/catch — that survives the v212 multi-strategy
    unwrap. Pin that path remains.
    """
    body = _app_js_body()
    assert 'console.warn("getLastConfigReceipt failed:"' in body, (
        "App.getLastConfigReceipt must log a warning when the unwrap "
        "raises — silent failure was the v212 symptom (the operator "
        "had to dig into the JS console to discover `proxy.sha` was "
        "undefined)."
    )
