## Why

The Pi already writes a rich runtime snapshot to `heart-matrix-controller/.status.json` (PID, active SHA, started_at, updated_at, uptime_seconds, mqtt_connected, last_tick_age_ms, messages_rendered, last_error — see `heart-matrix-controller/status.py:StatusSnapshot`) every 3s via the throttled `StatusWriter`. The loader reads that file to validate staged worktrees (`heart-matrix-controller/loader.py:probe`) — it's the **only** consumer. The Flask server has **zero visibility** into the sign's runtime state: there's no MQTT topic carrying the snapshot, no Flask subscriber, no API endpoint, and no UI element that shows real sign health.

Two pieces of UI actively misrepresent reality today:

1. **Dashboard page** has a static green "Live" pill (`templates/dashboard.html:10-13`) that is hardcoded HTML — it does not reflect any runtime signal from the sign. Operators have no way to tell whether the sign is actually rendering.
2. **Settings page** has no sign-health section at all — the operator can't see the running version, last-start time, MQTT-connected flag, or last error without SSHing to the Pi and reading `.status.json` themselves.

The fix is to publish the existing `StatusSnapshot` over MQTT on a dedicated status topic and surface it in the admin UI. The `StatusSnapshot` is already the right shape — schema-versioned, atomic, throttled — so this change is wiring (publish + subscribe + UI), not new state design.

## What Changes

- The Pi (the device side) **publishes** a serialized `StatusSnapshot` to a new MQTT status topic on a 30s cadence (separate from the 3s `.status.json` write — `.status.json` is consumed by the loader and stays at its existing cadence; the MQTT publish is for the admin UI and is throttled to a coarser cadence to avoid broker spam).
- The Flask server **subscribes** to that status topic and keeps the latest received snapshot in memory under a `threading.RLock` (the same pattern `SignConfig` uses for runtime config in `lib_shared/models.py:421`).
- A new `GET /api/sign-status` endpoint returns the latest snapshot (or `null` if none received in the last 2 minutes — "stale" means the sign is unreachable). The endpoint is JSON-only, no auth needed beyond the existing user-session check, and is consumed by JS for both the dashboard "Live" pill and the new Settings-page section.
- The Dashboard's hardcoded green "Live" pill becomes **dynamic**: green when the latest snapshot is <60s old, amber when 60-120s, grey/red when >120s or never received. The pulse animation is preserved for the green state to match the existing visual language.
- The Settings page gains a new **Sign Health** section under the existing form, showing the latest snapshot fields (running SHA, started_at, uptime, mqtt_connected, last_error) and the timestamp the snapshot was received. The values are read-only — operator controls live on a different page (or aren't wired in this change).
- `MQTT_STATUS_TOPIC` is added to both `settings.toml` files, with a documented derivation rule (the existing `MQTT_TOPIC` + `-status` suffix is the canonical default; operators can override). Environment variables take precedence.
- The Flask-side MQTT subscribe loop is extended to handle status-topic messages (a second `client.subscribe(STATUS_TOPIC)` call on a separate callback), so the existing `client.subscribe(MQTT_TOPIC)` for envelopes is unchanged. The status handler is a separate function that deserializes the snapshot and updates the in-memory `latest_status` lock-guarded dict.
- The Pi publishes via a new helper on `PahoMqttClient` (`publish_status(snapshot_dict)`) that follows the same `connect_event` + `loop_start()` + `result.wait_for_publish(timeout=5)` pattern as the existing `publish_envelope` method, but on the status topic with a fresh client per call (status publishes are infrequent — 30s — so a fresh client per call is simpler than a long-lived publisher and matches the existing pattern).

## Capabilities

### New Capabilities

- `sign-status-reports`: the Pi publishes a serialized `StatusSnapshot` (schema_version, pid, active_sha, started_at, updated_at, uptime_seconds, mqtt_connected, last_tick_age_ms, messages_rendered, last_error) to a new `STATUS_TOPIC` on a 30s cadence. Flask subscribes to that topic, stores the latest snapshot in an in-memory lock-guarded dict, and exposes it via `GET /api/sign-status`. The Dashboard's "Live" pill becomes real (green <60s, amber 60-120s, grey >120s or never), and the Settings page gets a new read-only **Sign Health** section showing the snapshot fields and the timestamp Flask received it. The status publish is fire-and-forget (QoS 0) so a flaky broker cannot stall the render loop; the loader's `.status.json` write (the existing 3s-throttled `StatusWriter`) is unchanged.

### Modified Capabilities

- *(none)* — the existing `MQTT_TOPIC` envelope publish/subscribe path is untouched. The status flow rides a separate topic and a separate handler.

## Impact

- **New files:**
  - `lib_shared/sign_status.py` — `LatestSignStatus` class: `threading.RLock`-guarded in-memory store, `update(snapshot_dict)`, `snapshot() -> dict | None`, `age_seconds() -> float | None`, `is_stale(threshold_s=120.0)`. The class is shared between Flask (the subscriber side) and a unit test that injects synthetic snapshots and asserts `age_seconds()` and `is_stale()`. Lives in `lib_shared/` rather than `heart-message-manager/` because the same dataclass shape (the existing `StatusSnapshot` from `heart-matrix-controller/status.py`) is the wire shape — `lib_shared/` keeps the broker payload independent of either runtime.
  - `heart-message-manager/static/sign_status.js` — small JS module loaded on the dashboard + settings pages: polls `GET /api/sign-status` every 10s, updates the `#sign-live-pill` element's text/color/animation, and renders the Settings-page Sign Health fields. Reuses the existing `#mqtt-status` styling primitives (the pill shape, the pulse animation class) so the UI vocabulary stays consistent.
  - `tests/test_sign_status.py` — round-trip tests for `LatestSignStatus`: `update()` stores the dict under the lock, `snapshot()` returns a defensive copy, `age_seconds()` returns the right number with a fake clock, `is_stale()` flips at the threshold, and `snapshot()` on an empty store returns `None`.
- **Modified files:**
  - `heart-matrix-controller/status.py` — add a `snapshot.to_mqtt_dict()` method that returns the wire shape (drops `pid`, which is host-local and not useful to the Flask UI; keeps all other fields). The dataclass is unchanged; this is a serialization helper.
  - `heart-matrix-controller/main.py` — add a new `_status_publisher` that, every 30s (separate `threading.Timer` from the render loop), reads `_build_status_snapshot(...)` and publishes via `PahoMqttClient.publish_status(...)`. Throttle is wall-clock-based (`time.monotonic()` deltas), not event-count, so a slow tick doesn't change the publish cadence. The existing `status_writer.tick()` on the render loop is unchanged — the loader still reads `.status.json` at its 3s cadence.
  - `lib_shared/paho_mqtt_client.py` — add `publish_status(payload: dict, topic: str) -> bool`: identical structure to the existing `publish_envelope` (fresh `mqtt.Client` per call, `connect_event` + `loop_start` + `result.wait_for_publish(timeout=5)`), but takes a raw dict + topic instead of an envelope, and uses QoS 0 (fire-and-forget) so a slow broker can't stall the render loop. Returns True on success. The existing `publish_envelope` is unchanged.
  - `heart-message-manager/main.py` — add `GET /api/sign-status` endpoint returning `latest_status.snapshot()` (or 204 if `is_stale()`); pass a `status_callback` to `PahoMqttClient` that calls `latest_status.update(parsed)`; add the `MQTT_STATUS_TOPIC` config key to `_cfg_required_keys`. The `dispatch_callback` for the existing `MQTT_TOPIC` (envelopes) is unchanged.
  - `heart-message-manager/templates/dashboard.html` — replace the hardcoded green "Live" pill (`<span class="...">Live</span>`) with a `<span id="sign-live-pill" data-state="unknown">…</span>` placeholder that `sign_status.js` populates. The pulse animation is gated on `data-state="live"`.
  - `heart-message-manager/templates/base.html` — add a single `<script src="{{ url_for('static', filename='sign_status.js') }}" defer></script>` tag in the `{% block scripts %}` area so the dashboard + settings pages share the live poll. The existing `#mqtt-status` (browser MQTT-WS connection status) stays untouched — that's a different signal.
  - `heart-message-manager/templates/settings.html` — add a new **Sign Health** section at the top (above "Sign Name") showing the running SHA, started_at, uptime (formatted as `Xd Yh Zm`), mqtt_connected, last_error, and the local timestamp the snapshot was received. The values are wrapped in `<span data-sign-status-field="...">` slots that `sign_status.js` populates on poll; the section has a small `<span data-sign-status-state>` indicator mirroring the dashboard pill state.
  - `heart-message-manager/settings.toml.example` — add `MQTT_STATUS_TOPIC = ""` (empty = default to `{MQTT_TOPIC}-status`), plus the standard env-override note.
  - `heart-matrix-controller/settings.toml.example` — same.
  - `heart-message-manager/templates/base.html` `window.APP_CONFIG` injection — surface the new `MQTT_STATUS_TOPIC` so the JS poll can show it in the WS-target row (mirroring the existing `MQTT_TOPIC` row) for debugging.
- **No new dependencies.** `threading.Timer` is stdlib. `MQTT_STATUS_TOPIC` is config-only.
- **No `.status.json` change** — the loader still reads the 3s-throttled file. The MQTT publish is a second consumer of the same `StatusSnapshot` shape.
- **No MQTT wire-shape change for envelopes** — the `MessageEnvelope` JSON shape (`type` + `payload`) is untouched. Status publishes are raw dicts, not envelopes, on a different topic.