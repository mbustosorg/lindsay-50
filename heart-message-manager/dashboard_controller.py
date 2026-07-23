"""Dashboard runtime controller — issue #48.

Owns one "runtime generation" of the simulated-Pi runtime:

  - shared Python `MessageManager` (constructed fresh per generation)
  - in-memory bounded `EventLog` (constructed fresh per generation)
  - browser-side MQTT-over-WebSocket subscriber (constructed fresh
    per generation; closed on Stop)
  - shared Python `EffectsCoordinator` + render layer (canvas +
    scroller + effects) bound on construction, released on Stop
  - per-generation listener registrations on `window.App.registerOnChange`

The controller drives a tiny state machine:

    stopped  ──Start()──▶  starting  ──ready──▶  running
                                                      │
                                                Stop()│
                                                      ▼
                                                  stopping ──▶ stopped

`error` is an off-band state the controller falls into from `starting`
when any bootstrap step fails. The operator clicks Start again to
construct a fresh generation; `error` is never recovered in-place.

Generation-discriminator pattern (per `feedback_one_shot_guards_need_discriminator.md`):

Every async callback (MQTT envelope, REST seed completion, MQTT
status, render-frame request, modal-scoped data request) is gated on
`self._generation_id`. When the generation id no longer matches
`self._active_generation_id`, the callback short-circuits without
mutating any state. The bool-and-id pattern survives a Stop-then-Start
race where a delayed MQTT envelope from the prior generation fires
after a new generation has taken over.

The Stop teardown is intentionally full — by design (per the spec):
  - The MQTT-WS client is `close()`d (the underlying shim closes the
    WebSocket and refuses to reconnect).
  - The Python `MessageManager` is replaced with a fresh stub
    (`_null_manager`) so any stale `_message_manager.dispatch(...)`
    call lands on a do-nothing shim.
  - The in-memory browser selector event log is dropped — the next
    generation constructs a new one.
  - The coordinator + scroller + canvas + render loop are released;
    `coord.unbind()` (added in lib_shared) clears the references so
    any leaked rAF tick is a no-op.
  - Timer/listener registrations on `window.App.registerOnChange` are
    removed — the next generation's listeners are the only ones
    active.

The instance-level state lives on `_runtime` (the active `Runtime`
record). When the runtime is `None`, the controller is "stopped" and
every public surface returns an empty / neutral answer.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

log = logging.getLogger("heart")


# Behavioral knobs — module-level constants (per
# `feedback_behavioral_knobs_in_code.md`). These are not settings.toml
# values because they describe the dashboard runtime's own
# implementation, not per-deployment operational values.

# Wait timeout for the initial app_main.py boot. 30 s covers a cold
# PyScript load (micropip + tzdata + numpy + Pillow + the heavy
# MessageManager import) without making a hung runtime feel like a
# hang forever.
BOOT_READY_TIMEOUT_S = 30.0

# Cap on the dashboard's in-memory browser selector event log.
# Matches the spec's "default cap 100 entries, FIFO drop-oldest".
DEFAULT_EVENT_LOG_MAX_ENTRIES = 100

# How many generations the controller remembers for diagnostics.
# Generations older than this are dropped from the in-memory history.
GENERATION_HISTORY_MAX = 8


# --- Runtime record ---------------------------------------------------------


@dataclass
class Runtime:
    """One simulated-Pi runtime generation.

    Holds every object the controller constructed for this generation,
    so `Stop` can release them in one place. Fields are populated
    incrementally as `Start()` walks the bootstrap sequence; partial
    fields are fine if a later step fails (the cleanup pass walks the
    same fields and releases whatever was set).
    """

    generation_id: int
    # The MessageManager the JS side talks to via `dispatch()`. Swapped
    # to a do-nothing stub on Stop so stale envelopes land safely.
    message_manager: Any
    # The in-memory bounded event log the selector consumes.
    event_log: Any
    # The MQTT-WS client; `close()`d on Stop.
    mqtt_ws_client: Any
    # The coordinator that drives per-frame work.
    coordinator: Any
    # Canvas + scroller + effects + heart — the render layer.
    display: Any
    scroller: Any
    effects: list
    heart: Any
    # Bound Python callback proxies we passed to JS — held so Stop
    # can release them.
    proxies: dict = field(default_factory=dict)
    # Cleanup callbacks registered by sub-modules (preview.js tick
    # loop cancellation, etc.).
    cleanup_callbacks: list = field(default_factory=list)
    # State for this generation. One of:
    #   "starting"   — construction in progress
    #   "running"    — bootstrap complete, ready to render
    #   "stopping"   — teardown in progress
    #   "stopped"    — fully torn down (this `Runtime` is about to be
    #                   dropped from the controller)
    state: str = "starting"
    # Last actionable error message captured during construction.
    error: Optional[str] = None


class _NullMessageManager:
    """A no-op MessageManager stand-in installed during Stop.

    Stale MQTT envelopes and stale REST seed completion callbacks
    arriving after Stop still call `dispatch()` /
    `take_next_new_message()` — without a stand-in, a `None` would
    crash. This stub absorbs the call, fires no `on_change`, and
    returns safe empty answers.

    Every method mirrors the real `MessageManager` surface the
    dashboard touches post-Stop: dispatch is silent, get_messages
    returns [], get_config returns a default SignConfig. Anything
    else (e.g. seed, register_handler) is also a no-op.
    """

    def __init__(self) -> None:
        self.config = None
        self.messages = None

    def dispatch(self, raw) -> None:  # noqa: ARG002 — drop on the floor
        """Drop the envelope on the floor; the generation is gone."""
        return None

    def get_messages(self, limit=None, suppress=True):  # noqa: ARG002 — no rows to return
        return []

    def get_config(self):
        return None

    def take_next_new_message(self):
        return None

    def register_handler(self, action, handler) -> None:  # noqa: ARG002 — handlers are dropped
        return None

    def seed(self):
        """Await-able no-op."""
        async def _noop():
            return None

        return _noop()


# --- Controller -------------------------------------------------------------


class DashboardController:
    """State machine + per-generation runtime for the standalone dashboard.

    Owns:
      - the active `Runtime` (None when stopped)
      - the generation discriminator (`_active_generation_id`)
      - a small ring of recent generations for diagnostics
      - the boot-ready flag (gates the auto-Start at page load)

    Public surface (mounted on `window.Dashboard` by app_main.py):

      - `state() -> str` — current state string for UI binding
      - `generation_id() -> int` — current generation id (or 0 if stopped)
      - `start() -> None` — synchronously begin a new generation
      - `stop() -> None` — synchronously tear down the current generation
      - `restart() -> None` — atomic Stop-then-Start
      - `invalidate() -> None` — test hook: cancel the active
        generation without teardown (used to simulate the
        "delayed callback from an old generation" scenario).
      - `submit_test_message(form_data) -> dict` — POST to Flask +
        return `{accepted: bool, http_status: int, error?: str}`
      - `open_modal(kind) -> None` — request a diagnostic modal
        (the JS side handles the actual DOM)

    Threading: the controller is single-threaded by default (PyScript
    runs on the browser event loop). The internal `_lock` guards the
    rare case where the Stop is invoked from a callback that fires
    concurrently with a Start.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._runtime: Optional[Runtime] = None
        self._next_generation_id = 1
        self._active_generation_id = 0
        self._generation_history: list[Runtime] = []
        # Map of test-message submissions in flight, keyed by an
        # internal id. When the corresponding MQTT envelope lands we
        # resolve the promise. Generation discriminator prevents a
        # late envelope from resolving an older submission's promise.
        self._pending_injections: dict[str, dict] = {}
        # Hooks the dashboard page wires in (canvas render loop, etc.).
        # The controller invokes them at the appropriate lifecycle
        # transitions; if a hook is None, the transition is a no-op
        # (test environments and the host CPython test suite have no
        # canvas).
        self._render_loop_start: Optional[Callable[[Runtime], None]] = None
        self._render_loop_stop: Optional[Callable[[Runtime], None]] = None

    # --- Public lifecycle surface -----------------------------------------

    def state(self) -> str:
        with self._lock:
            if self._runtime is None:
                return "stopped"
            return self._runtime.state

    def generation_id(self) -> int:
        with self._lock:
            return self._active_generation_id

    def runtime(self) -> Optional[Runtime]:
        """Return the active runtime — test-only diagnostic accessor.

        Production code paths never read the runtime directly; they
        call the controller's `state()` / `generation_id()` helpers
        or the JS-callable `App.getMessages` / `App.getConfig`
        proxies that the controller wires up.
        """
        with self._lock:
            return self._runtime

    def set_render_loop_hooks(
        self,
        on_start: Optional[Callable[[Runtime], None]] = None,
        on_stop: Optional[Callable[[Runtime], None]] = None,
    ) -> None:
        """Install render-loop start/stop hooks.

        The JS side registers these so `Start` resumes the rAF loop
        and `Stop` cancels it. The hooks are pure-Python callables —
        the JS side passes proxies for whatever browser-side logic
        it needs to drive.
        """
        self._render_loop_start = on_start
        self._render_loop_stop = on_stop

    def start(self) -> None:
        """Synchronously begin a new generation.

        Idempotent: a second Start while a generation is alive is a
        no-op (logged). A Start while in `stopping` is queued
        implicitly because Stop blocks until teardown completes.
        """
        with self._lock:
            if self._runtime is not None and self._runtime.state in ("starting", "running"):
                log.info("DashboardController.start: ignored (state=%s)", self._runtime.state)
                return
            gen_id = self._next_generation_id
            self._next_generation_id += 1
            self._active_generation_id = gen_id
            # The actual runtime construction is async (REST seed).
            # `starting` is the optimistic state; if the bootstrap
            # fails, the state moves to `error` and the runtime is
            # released.
            self._runtime = Runtime(
                generation_id=gen_id,
                message_manager=_NullMessageManager(),
                event_log=None,
                mqtt_ws_client=None,
                coordinator=None,
                display=None,
                scroller=None,
                effects=[],
                heart=None,
                state="starting",
            )

        # Bootstrap outside the lock so the lock isn't held during
        # the REST seed / MQTT connect / coordinator bind. The state
        # flips to `running` only after every step succeeds.
        try:
            self._bootstrap_generation(self._runtime_for_generation(gen_id))
        except Exception as e:  # noqa: BLE001 — we want to capture every failure
            log.exception("DashboardController.start: bootstrap failed: %s", e)
            self._mark_error(gen_id, str(e))

    def stop(self) -> None:
        """Synchronously tear down the active generation.

        Idempotent: Stop on a stopped controller is a no-op. A Stop
        while a generation is mid-bootstrap stops the bootstrap
        cleanly (no callbacks will resolve past the discriminator).

        Ordering — design §2 calls for "invalidate the generation
        first, then tear down". We zero `_active_generation_id`
        *before* running teardown so any wrapped async callback
        (the `_on_change_js` closure that captures `gen_id` at
        wrap time) sees `is_active_generation(gen_id) is False`
        immediately and short-circuits without mutating JS-side
        state. Unwrapped callbacks (e.g. `_on_envelope_js`) are
        caught by the per-call `_NullMessageManager` swap inside
        `_teardown_generation`.
        """
        with self._lock:
            runtime = self._runtime
            if runtime is None:
                return
            # Invalidate the generation FIRST so wrapped callbacks
            # short-circuit before we touch any teardown state.
            # Without this, a late `_on_change_js` (capturing the
            # still-valid gen_id) could fan out via
            # `App._dispatchChange()` during the brief window
            # between `runtime.state = "stopping"` and the eventual
            # zeroing of `_active_generation_id`.
            self._active_generation_id = 0
            runtime.state = "stopping"
        # Render-loop stop runs outside the lock — it may touch
        # `requestAnimationFrame` cancel which is a JS-side API.
        try:
            if self._render_loop_stop is not None:
                self._render_loop_stop(runtime)
        except Exception as e:
            log.warning("DashboardController.stop: render loop stop failed: %s", e)
        # Tear down the runtime resources (swap MM to NullMM,
        # close MQTT, drop event log, release coord/scroller/canvas).
        self._teardown_generation(runtime)
        # Clear the active slot.
        with self._lock:
            # Idempotence: if Start was called during the teardown,
            # the new generation already owns the slot. Don't clobber.
            if self._runtime is runtime:
                self._runtime = None
            runtime.state = "stopped"
            # Keep the torn-down record in the history ring for
            # diagnostics.
            self._record_history(runtime)

    def restart(self) -> None:
        """Atomic Stop-then-Start. Returns once the new generation
        has reached `starting` (the bootstrap itself is async)."""
        self.stop()
        self.start()

    def is_active_generation(self, generation_id: int) -> bool:
        """The discriminator every async callback consults.

        Returns True iff the given `generation_id` is the current
        active generation. Returns False for any prior generation,
        including one that has already been torn down.
        """
        with self._lock:
            return generation_id == self._active_generation_id

    def invalidate(self, generation_id: int) -> None:
        """Test-only: mark a generation as no-longer-active without
        running the full teardown.

        The production Stop runs `_teardown_generation` and clears
        the runtime; tests for the stale-callback rejection path
        want to set up the discriminator mismatch without paying
        for the resource release. The production stop also clears
        `_active_generation_id` — `invalidate` does the same so the
        callback rejection path is faithful.
        """
        with self._lock:
            if generation_id == self._active_generation_id:
                self._active_generation_id = 0
                if self._runtime is not None and self._runtime.generation_id == generation_id:
                    self._runtime = None

    # --- Generation history -------------------------------------------------

    def _record_history(self, runtime: Runtime) -> None:
        """Append a torn-down runtime to the bounded history ring."""
        with self._lock:
            self._generation_history.append(runtime)
            if len(self._generation_history) > GENERATION_HISTORY_MAX:
                self._generation_history = self._generation_history[-GENERATION_HISTORY_MAX:]

    def history_snapshot(self) -> list[dict]:
        """Return a JSON-safe view of the recent-generation history.

        Used by the diagnostics panel / future tests. Strips the
        runtime objects to their `(generation_id, state, error)`
        summary so the wire form is small.
        """
        with self._lock:
            return [
                {"generation_id": r.generation_id, "state": r.state, "error": r.error}
                for r in self._generation_history
            ]

    # --- Helpers used by Start() / Stop() ----------------------------------

    def _runtime_for_generation(self, generation_id: int) -> Runtime:
        """Return the runtime iff the id matches the active generation.

        Used by the bootstrap coroutine to confirm its target is
        still alive before each step completes.
        """
        with self._lock:
            if self._runtime is None:
                raise RuntimeError(f"runtime for generation {generation_id} is gone")
            if self._runtime.generation_id != generation_id:
                raise RuntimeError(
                    f"runtime for generation {generation_id} was superseded by "
                    f"{self._runtime.generation_id}"
                )
            return self._runtime

    def _mark_error(self, generation_id: int, error: str) -> None:
        """Move a failed bootstrap into the error state.

        Releases the partially-constructed runtime so the next Start
        can build from a clean slate.
        """
        with self._lock:
            runtime = self._runtime
            if runtime is None or runtime.generation_id != generation_id:
                return
            runtime.state = "error"
            runtime.error = error
            self._record_history(runtime)
            self._runtime = None
            self._active_generation_id = 0

    def _teardown_generation(self, runtime: Runtime) -> None:
        """Release the runtime's resources in dependency order.

        Order matters:
          1. Run user-supplied cleanup callbacks (e.g. rAF cancel)
          2. Swap the MessageManager to a null stub so stale
             dispatches land on the no-op
          3. Close the MQTT-WS client (refuses reconnects)
          4. Drop the in-memory event log
          5. Release the coordinator + scroller + canvas references
          6. Drop the proxy registry
        """
        for cb in list(runtime.cleanup_callbacks):
            try:
                cb()
            except Exception as e:
                log.warning("DashboardController teardown: cleanup callback raised: %s", e)
        runtime.cleanup_callbacks.clear()
        runtime.message_manager = _NullMessageManager()
        if runtime.mqtt_ws_client is not None:
            try:
                runtime.mqtt_ws_client.close()
            except Exception as e:
                log.warning("DashboardController teardown: mqtt close failed: %s", e)
            runtime.mqtt_ws_client = None
        if runtime.event_log is not None:
            try:
                runtime.event_log.clear()
            except Exception as e:
                log.warning("DashboardController teardown: event log clear failed: %s", e)
            runtime.event_log = None
        # Coordinator/scroller/canvas: drop the references. The
        # underlying objects become unreferenced and are GC'd; any
        # leaked tick call after this is a no-op because
        # `_coord_ref["coord"]` was reset to None.
        runtime.coordinator = None
        runtime.scroller = None
        runtime.display = None
        runtime.effects = []
        runtime.heart = None
        runtime.proxies.clear()

    def _bootstrap_generation(self, runtime: Runtime) -> None:
        """Bootstrap the runtime. Stub for the host test suite.

        The PyScript-rendered dashboard overrides this via
        `set_render_loop_hooks` / a Python entry point that
        constructs the MessageManager, runs `seed()`, opens the
        MQTT-WS connection, and binds the coordinator. The host
        test suite exercises the state machine without rendering
        anything, so the default implementation just flips state
        to `running` if no hooks are installed.
        """
        # If a render-loop start hook is installed, run it. The hook
        # is responsible for constructing the MessageManager +
        # event_log + coordinator + render layer and updating
        # `runtime.*` in place.
        if self._render_loop_start is not None:
            try:
                self._render_loop_start(runtime)
            except Exception as e:
                log.exception("DashboardController bootstrap: render loop start failed: %s", e)
                self._mark_error(runtime.generation_id, str(e))
                return
        # If the hook installed an event_log + message_manager, flip
        # to running. If they weren't installed, the runtime stays
        # in `starting` (the host test suite expects this — there's
        # no render layer to bind).
        if runtime.message_manager is not None and not isinstance(runtime.message_manager, _NullMessageManager):
            runtime.state = "running"
