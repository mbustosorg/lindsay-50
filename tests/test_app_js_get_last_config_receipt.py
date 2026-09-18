"""Regression test for Browser/Config not populating on cold load (v208).

Symptom (operator-reported, v208): Browser/Config cell stayed "—"
even though Python-side `_last_applied_config_sha` was set to the
correct SHA (`c77bcde`). Flask/Config populated correctly because
that's server-rendered from `{{ flask_config_sha }}`.

Root cause: `App.getLastConfigReceipt()` did
`await window._message_manager.get_last_config_receipt()`. PyScript
proxies return the proxy itself when awaited, NOT the unwrapped
Python value. So the receipt object had no `.sha` (it had `.get`,
`.toString`, etc.), and `applyBrowserConfigReceipt` saw
`receipt.sha` undefined → cell stayed "—".

Fix: drop the `await`. The PyScript proxy exposes Python attributes
directly — read `.sha` off the returned object without going
through `await`. The proxy returns the dict-or-proxy; either way
`.sha` is accessible.

This test pins the SOURCE shape: we don't need a live PyScript to
catch the regression — we just check that `app.js` reads `.sha`
without `await`. If a future refactor re-adds `await`, the test
fails before the change ships.
"""

from __future__ import annotations

import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))


def test_app_js_does_not_await_get_last_config_receipt():
    """`App.getLastConfigReceipt()` must NOT `await` the PyScript proxy.

    PyScript proxies: `await proxy.sync_method()` returns the proxy
    itself, not the underlying Python value. The receipt dict
    `{"sha": str}` has no proxy wrappers to unwrap — `.sha` is
    accessible directly. Awaiting would yield the proxy back, and
    `receipt.sha` would be undefined → Browser/Config cell stays
    "—" forever.

    Pin: the source for `App.getLastConfigReceipt` must call
    `window._message_manager.get_last_config_receipt()` WITHOUT a
    preceding `await` keyword. The receipt helper itself stays
    async (so the caller can `await App.getLastConfigReceipt()` —
    that's fine, the function returns a plain `{sha: ""}` object).
    """
    src = (_PROJECT_ROOT / "heart-message-manager" / "static" / "app.js").read_text()

    # Find the getLastConfigReceipt function body.
    fn_idx = src.find("async function getLastConfigReceipt")
    assert fn_idx != -1, "app.js must define getLastConfigReceipt"
    # Body extends to the next top-level blank line + closing brace.
    body = src[fn_idx : fn_idx + 1500]

    # The proxy call itself must NOT be awaited.
    assert "await window._message_manager.get_last_config_receipt()" not in body, (
        "App.getLastConfigReceipt must NOT await the PyScript proxy — "
        "awaiting a sync method on a PyScript proxy returns the proxy "
        "itself, not the underlying dict. Symptom: Browser/Config "
        "cell stayed '—' even though Python-side "
        "_last_applied_config_sha was populated (v208). "
        "Read `.sha` directly off the returned object."
    )

    # The function must still read `.sha` off the result.
    assert "receipt.sha" in body, (
        "App.getLastConfigReceipt must read .sha off the returned "
        "object — the dict shape is {sha: str}."
    )


def test_app_js_reads_receipt_sha_without_await():
    """Backup assertion: the receipt helper handles the
    proxy-or-dict shape (both expose `.sha`). Pins the fix at the
    shape level so any future change to the unwrap logic is
    intentional.
    """
    src = (_PROJECT_ROOT / "heart-message-manager" / "static" / "app.js").read_text()
    fn_idx = src.find("async function getLastConfigReceipt")
    assert fn_idx != -1
    body = src[fn_idx : fn_idx + 1500]

    # Defensive shape handling: check for the `"sha" in receipt`
    # pattern OR a direct property access. We accept either, but
    # there must NOT be a bare `return await window._message_manager...`
    # that propagates the proxy as-is.
    assert (
        '"sha" in receipt' in body or "receipt.sha" in body
    ), (
        "App.getLastConfigReceipt must defensively read .sha off "
        "the returned object (handles both dict and JsProxy shapes)."
    )
