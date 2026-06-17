"""Tests that the push path is gone from `EffectsCoordinator` and the JS surface.

The push path (the JS-driven `request_message` / `apply_config` calls
and the coordinator's `set_text` / `pending_text` / `startup_text` API)
was removed in issue #44 — derived state now flows through the
MessageManager's universal `on_change` callback. These tests assert
the removal happened cleanly:

1. `coordinator.set_text("hello")` raises `AttributeError`.
2. Reading `coordinator.pending_text` raises `AttributeError`.
3. `coordinator.start("seed")` raises `TypeError` (extra positional arg).
4. The browser surface: `window.request_message` and `window.apply_config`
   are no longer installed by `preview_main.py` (static check of the
   source file).
5. The JS surface: `preview.js` does not contain `reRender`,
   `registerOnChange`, `request_message`, or `apply_config` as a
   function or call site (static check of the source file).
4.6. `heart-matrix-controller/main.py` does not define
   `_on_config_update` or `_dispatch_with_config`, does not assign
   `_message_mgr.dispatch = _dispatch_with_config`, and does not
   call `coordinator.start(_startup_text)`.
"""

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from lib_shared.effects_coordinator import EffectsCoordinator
from lib_shared.models import EffectsSettings, TextSettings


# --- shared stubs ------------------------------------------------------------


class _StubCanvas:
    width = 64
    height = 64


class _StubDisplay:
    def __init__(self):
        self.width = 64
        self.height = 64
        self.canvas = _StubCanvas()

    def clear(self):
        pass

    def render(self, effect, scroller):
        pass


class _StubScroller:
    def __init__(self):
        self.text = ""
        self._color = 0xFF6400
        self.frame_delay = 0.040
        self.offset_seconds = 1.0

    def set_text(self, text, width):
        self.text = text

    def set_color(self, c):
        self._color = c

    def set_speed(self, s):
        self.frame_delay = 0.020 if s >= 5 else 0.080
        self.offset_seconds = 0.5 if s >= 5 else 1.5

    def set_brightness(self, b):
        pass

    def tick(self, w):
        pass

    def render(self, canvas):
        pass


def _make_effect(name):

    class _Fx:
        def __init__(self):
            self.brightness = 1.0

        def tick(self):
            pass

        def render(self, canvas):
            pass

        def set_brightness(self, b):
            self.brightness = b

    _Fx.__name__ = name
    return _Fx


class _StubMessageManager:
    def __init__(self):
        from types import SimpleNamespace

        self.messages = SimpleNamespace(get_messages=lambda limit=100, suppress=True: [])
        self.config = SimpleNamespace(
            effect_settings=EffectsSettings(), text_settings=TextSettings()
        )


def _build():
    mgr = _StubMessageManager()
    coord = EffectsCoordinator(
        message_manager=mgr,
        display=_StubDisplay(),
        scroller=_StubScroller(),
        effects=[_make_effect("A")(), _make_effect("B")()],
        heart=_make_effect("Heart")(),
    )
    return coord


# --- Scenario 1: set_text raises AttributeError ------------------------------


def test_set_text_raises_attributeerror():
    """`coordinator.set_text` is gone — calling it raises AttributeError."""
    coord = _build()
    with pytest.raises(AttributeError):
        coord.set_text("hello")


# --- Scenario 2: pending_text raises AttributeError --------------------------


def test_pending_text_raises_attributeerror():
    """`coordinator.pending_text` is gone — reading it raises AttributeError."""
    coord = _build()
    with pytest.raises(AttributeError):
        _ = coord.pending_text


# --- Scenario 3: start with extra positional arg raises TypeError ------------


def test_start_with_extra_arg_raises_typeerror():
    """`start(self)` takes no args — `start("seed")` raises TypeError."""
    coord = _build()
    with pytest.raises(TypeError):
        coord.start("seed")


def test_start_with_no_args_is_fine():
    """`start(self)` is callable with no args (the new contract)."""
    coord = _build()
    coord.start()
    # mode = "intro" is the start state.
    assert coord.mode == "intro"


# --- Scenario 4: preview_main.py drops window.request_message and apply_config


def test_preview_main_drops_request_message_and_apply_config():
    """The browser-facing `request_message` and `apply_config` are gone.

    Static check: the file should not define either function. We allow
    them to appear in the docstring (which documents the historical
    surface) — the assertion is against Python definitions.
    """
    p = Path(__file__).parent.parent.parent / "heart-message-manager" / "preview_main.py"
    src = p.read_text(encoding="utf-8")
    # No top-level def of `def request_message(`
    assert not re.search(r"^def\s+request_message\s*\(", src, re.MULTILINE), (
        "preview_main.py must not define `request_message`"
    )
    # No top-level def of `def apply_config(`
    assert not re.search(r"^def\s+apply_config\s*\(", src, re.MULTILINE), (
        "preview_main.py must not define `apply_config`"
    )
    # The bootstrap / install-js surface must NOT install them on window.
    # (The new `_install_js_api` only assigns `tick`, `get_frame_rgba`,
    # `get_current_text`, `get_current_effect_name`.)
    install_block = re.search(
        r"def\s+_install_js_api\s*\([^)]*\).*?(?=def\s+_coord\s*\(|def\s+tick\s*\()",
        src,
        re.DOTALL,
    )
    assert install_block is not None, "could not locate _install_js_api() block"
    install_body = install_block.group(0)
    assert "js.window.request_message" not in install_body, (
        "_install_js_api() must not assign request_message to window"
    )
    assert "js.window.apply_config" not in install_body, (
        "_install_js_api() must not assign apply_config to window"
    )


# --- Scenario 5: preview.js drops reRender / registerOnChange / etc. ---------


def test_preview_js_drops_reRender_and_registerOnChange():
    """The browser JS no longer pushes via reRender / registerOnChange / etc.

    Static check of `heart-message-manager/static/preview/preview.js`:
    the file must not contain function definitions or call sites for
    `reRender`, `registerOnChange`, `request_message`, or `apply_config`.
    The historical names are allowed to appear in comments that document
    the new contract (so readers know what was removed) — we strip
    comments first before checking.
    """
    p = (
        Path(__file__).parent.parent.parent
        / "heart-message-manager"
        / "static"
        / "preview"
        / "preview.js"
    )
    src = p.read_text(encoding="utf-8")
    # Strip line comments and block comments so the names appear ONLY
    # in explanatory prose, not as code.
    no_block = re.sub(r"/\*.*?\*/", "", src, flags=re.DOTALL)
    no_line = re.sub(r"//[^\n]*", "", no_block)
    # No function definition of `reRender` (e.g. `function reRender(`
    # or `async function reRender(`).
    assert not re.search(
        r"(?:async\s+)?function\s+reRender\s*\(", no_line
    ), "preview.js must not define reRender"
    # No call to registerOnChange.
    assert "registerOnChange" not in no_line, (
        "preview.js must not call window.App.registerOnChange"
    )
    # No call to request_message or apply_config.
    assert "request_message" not in no_line, (
        "preview.js must not call window.request_message"
    )
    assert "apply_config" not in no_line, (
        "preview.js must not call window.apply_config"
    )


# --- Scenario 4.6: heart-matrix-controller/main.py cleans up dispatch wrapping


def test_pi_main_cleans_up_dispatch_wrapping():
    """The Pi entrypoint no longer wraps MessageManager.dispatch.

    Static check of `heart-matrix-controller/main.py`:
    - No definition of `_on_config_update`.
    - No definition of `_dispatch_with_config`.
    - No assignment of `_message_mgr.dispatch = _dispatch_with_config`.
    - No call to `coordinator.start(_startup_text)`.
    """
    p = Path(__file__).parent.parent.parent / "heart-matrix-controller" / "main.py"
    src = p.read_text(encoding="utf-8")
    assert not re.search(r"^def\s+_on_config_update\s*\(", src, re.MULTILINE), (
        "heart-matrix-controller/main.py must not define _on_config_update"
    )
    assert not re.search(r"^def\s+_dispatch_with_config\s*\(", src, re.MULTILINE), (
        "heart-matrix-controller/main.py must not define _dispatch_with_config"
    )
    assert "_message_mgr.dispatch = _dispatch_with_config" not in src, (
        "heart-matrix-controller/main.py must not patch _message_mgr.dispatch"
    )
    assert "coordinator.start(_startup_text)" not in src, (
        "heart-matrix-controller/main.py must not call coordinator.start(_startup_text)"
    )
