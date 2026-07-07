## Context

The Pi already publishes rich runtime state to a local file (`.status.json` at the repo root) every ~3 seconds, via the throttled `StatusWriter` in `heart-matrix-controller/status.py`. That snapshot covers everything the issue asks for and more: `pid`, `active_sha`, `started_at`, `updated_at`, `uptime_seconds`, `mqtt_connected`, `last_tick_age_ms`, `messages_rendered`, `last_error`. The file is consumed **only** by the loader (`heart-matrix-controller/loader.py`) to decide whether to swap a staged worktree. The Flask server has no MQTT topic carrying this state, no subscriber, and no UI surface for it.

The codebase already has a pattern for "device → Flask over MQTT" that this change can mirror cleanly: the existing `MQTT_TOPIC` carries `MessageEnvelope`s (`type` + `payload`); both sides already wire a `PahoMqttClient` with a `dispatch_callback`. The status flow uses a separate topic and a separate callback so the existing envelope path stays untouched.

There are also two existing UI patterns that surface a similar "live signal":
- `#mqtt-status` in `templates/base.html:166` — pill driven by `static/mqtt_ws_client.js`, showing the **browser's** WebSocket connection status to the Flask MQTT-WS bridge (the operator's browser connected to the broker). This is NOT the same signal as "is the Pi alive" — it's "is the operator's browser talking to the broker."
- The dashboard "Live" pill (`templates/dashboard.html:10-13`) — hardcoded green, no runtime signal.

The new `sign-status-reports` capability sits between these: the Pi publishes a runtime snapshot; Flask subscribes; the dashboard pill and the new Settings-page section both render the latest snapshot. The existing `#mqtt-status` is untouched (it's still a separate, useful "browser → broker" signal).

## Goals / Non-Goals

**Goals:**

- The Pi publishes its existing `StatusSnapshot` over MQTT on a dedicated status topic, throttled to a 30s cadence (separate from the 3s `.status.json` write — the loader's file write cadence is unchanged).
- Flask subscribes to that status topic and keeps the latest snapshot in a `threading.RLock`-guarded in-memory store.
- `GET /api/sign-status` returns the latest snapshot (or 204 when stale or never received) and is consumed by both the Dashboard pill and the new Settings-page section.
- The Dashboard's static "Live" pill becomes real: green when the snapshot is <60s old, amber when 60-120s, grey (with `data-state="unknown"`) when >120s or never received. The pulse animation is preserved for the green state.
- The Settings page gets a new read-only **Sign Health** section at the top (above "Sign Name") with the snapshot fields and the timestamp Flask received the snapshot.

**Non-Goals:**

- No new runtime health metric — the snapshot is exactly the existing `StatusSnapshot` shape, no new fields are added. If the issue author wants a new metric later (e.g., "free disk space"), that's a follow-up change to the snapshot schema in `status.py`.
- No broker-side persistence — Flask keeps only the latest snapshot in memory. Historical snapshots ("show me the last 24 hours") are out of scope.
- No push channel to the browser. The browser polls `GET /api/sign-status` every 10s; no WebSocket, no Server-Sent Events. This matches the existing `setInterval(fetchMessages, 3000)` pattern in `templates/testing.html` and the polling pattern already used by `add-sign-preview-rendering`.
- No changes to the existing `MessageEnvelope` wire shape or the existing `MQTT_TOPIC` subscribe path.
- No `.status.json` change — the loader's 3s-throttled file write is unchanged; the MQTT publish is a second consumer of the same dataclass.
- No change to the existing `#mqtt-status` browser-WS pill. That signal stays as it is.
- No new dependencies. `threading.Timer` and `threading.RLock` are stdlib.

## Decisions

### 1. Status publish cadence: 30s wall-clock, not 3s like the file write

The 3s `.status.json` cadence is the loader's contract — a fresh file proves the app is alive and rendering, which is what the loader probes every ~8s before swapping. The MQTT publish is the admin UI's contract — a human watching the dashboard doesn't need 3s granularity, and 3s × 24h × N signs of broker writes is wasteful. 30s is a balance: a missed publish is noticeable within ~60s on the dashboard (the green/amber boundary), and the broker load is 1 message per minute per sign.

**Alternative considered:** publish on every render-loop tick (like the `.status.json` writer) and let the broker drop duplicates. Rejected — broker writes are more expensive than local `os.replace`, and the UI doesn't benefit from sub-30s resolution.

**Alternative considered:** publish only on state change (mqtt_connected flips, last_error appears, active_sha changes). Rejected — operators want a "last seen" signal, and silent absence on a healthy sign is ambiguous (is the broker broken, or is the sign actually idle?). A 30s heartbeat is unambiguous: missing heartbeat = unreachable.

### 2. Separate MQTT topic, not a `type="status"` envelope

The existing `MQTT_TOPIC` carries `MessageEnvelope`s with `type` ∈ `{"message", "config"}`. Adding `type="status"` to that schema would force every consumer to handle the new type, including the browser MQTT-WS bridge (which already has its own dispatch logic in `static/mqtt_ws_client.py`). A separate topic with a separate handler keeps blast radius small: the envelope consumer doesn't change, the status consumer is a new function, and a misconfigured broker only breaks one flow.

**Alternative considered:** a `type="status"` envelope on the existing topic. Rejected for the blast-radius reason above. The cost of a separate topic (one more broker subscription) is negligible.

**Topic derivation:** `MQTT_STATUS_TOPIC` defaults to `{MQTT_TOPIC}-status` (e.g., `mbustosorg/feeds/lindsay-50-status` on Adafruit IO). Operators can override via `settings.toml` or env var. Adafruit IO requires explicit feed creation — `lindsay-50-status` would need to be created in the AIO dashboard before the first publish lands. This is documented in the new `settings.toml.example` lines for both server and device.

### 3. QoS 0 (fire-and-forget) for status publishes

The render loop on the Pi is the producer; it can't afford to block on a slow broker. The status publish runs in a `threading.Timer` callback (separate from the render loop), but a network stall could still delay the next timer firing. QoS 0 means "best effort" — `result.wait_for_publish(timeout=5)` is removed entirely; the publish returns immediately after `client.publish(...)` enqueues the message. A missed status publish is self-healing on the next 30s tick. The Flask side uses QoS 1 on `subscribe` (matches the existing `client.subscribe(topic)` which defaults to QoS 1) so Flask doesn't miss a snapshot because of a transient network blip.

**Alternative considered:** QoS 1 on both sides. Rejected — the producer's QoS 1 contract is "I will retry until PUBACK," which is exactly the blocking behavior we're trying to avoid.

### 4. `LatestSignStatus` in `lib_shared/`, not Flask-only

The class is small (~5 methods) but the lock-guarded in-memory store pattern is shared with `SignConfig` (`lib_shared/models.py:421`). Putting `LatestSignStatus` in `lib_shared/` lets the unit test inject synthetic snapshots and exercise the lock + staleness logic without spinning up Flask. Putting it under `heart-message-manager/` would force the test to construct a Flask app context to test a thread-safe store — overkill.

**Alternative considered:** Flask-only in `heart-message-manager/sign_status.py`. Rejected — the dataclass-shaped wire format is the broker payload, not a Flask-internal concern.

### 5. Fresh paho client per status publish

The existing `PahoMqttClient.publish_envelope` already opens a fresh `mqtt.Client` per call (the docstring explains why — paho's `loop_start` is required for the `wait_for_publish` to resolve, and a long-lived publisher would need careful lifecycle management). The new `publish_status(payload, topic)` follows the same pattern. Status publishes are infrequent (every 30s), so the per-call handshake cost (~50-200ms) is negligible.

**Alternative considered:** long-lived paho publisher on the Pi running in a daemon thread. Rejected — adds a second daemon thread to the Pi's already-threaded process, and `publish_envelope` already established the fresh-client-per-call pattern as the convention.

### 6. JS-side polling at 10s, not matching the 30s publish cadence

The browser polls `GET /api/sign-status` every 10s. The cadence is **faster** than the publish cadence (10s poll vs 30s publish) so a missed publish shows up within ~30s instead of ~60s. The trade-off is extra GET requests; for an admin UI that's open in one operator's tab, this is trivial.

**Alternative considered:** match the publish cadence (poll every 30s). Rejected — adds 30s of latency to the "stale" detection on the dashboard.

### 7. The `pid` field is dropped from the wire format

The dataclass keeps `pid` (the OS-level PID of the running app). The Flask UI doesn't care which OS PID is rendering — it cares about `active_sha`, `started_at`, `uptime_seconds`, `mqtt_connected`, `last_error`. `pid` is host-local and is consumed by the loader for diagnostics, not by Flask. `StatusSnapshot.to_mqtt_dict()` drops it; `StatusSnapshot.to_dict()` (used for `.status.json`) keeps it.

**Alternative considered:** send everything over the wire and let Flask ignore `pid`. Rejected — explicit drop is the documentation; future readers of the wire shape don't wonder "what's this pid field for?"

## Risks / Trade-offs

- **Stale-snapshot UI lies** — the dashboard pill says "Live" if the snapshot is <60s old. If the Pi freezes but keeps the render loop alive enough to publish (improbable but possible), the UI says green when the sign is actually frozen. → Mitigation: the loader's separate `.status.json` probe still detects a stuck render loop (no fresh file mtime = no swap); the dashboard's "Live" pill is a UI signal, not a health check. If the operator sees green but suspects a problem, the `.status.json` mtime on the Pi is the authoritative signal.

- **Broker outage masks Pi outage** — if the broker is down, Flask stops receiving snapshots, and the dashboard pill goes grey within 120s. The operator can't tell whether the sign or the broker is at fault. → Mitigation: the existing `#mqtt-status` pill (browser → broker WS) is still visible in the header; if both go grey simultaneously, the broker is the suspect. Document this in the Settings page Sign Health section ("Status updates require the MQTT broker to be reachable").

- **Pi render-loop regression under broker load** — even at QoS 0, the fresh-client-per-call status publish opens a TCP connection every 30s. On a constrained Pi 4 this could add up if the broker has a slow handshake. → Mitigation: the existing `publish_envelope` already does the same thing on every inbound SMS (which is rare); 30s is the absolute worst case for status. If broker latency is measured to be a problem, future change can switch to a long-lived publisher.

- **Snapshot schema drift** — if `StatusSnapshot` gains a new field (e.g., `free_disk_mb`), the broker-side Flask subscriber will see a dict it doesn't recognize. → Mitigation: `LatestSignStatus.update()` does a defensive merge (only known keys land in the in-memory store; unknown keys are logged at INFO and dropped). This is the same pattern as `SignConfig.from_dict`'s "ignore unknown keys" approach.

- **Two subscribers on the same Flask process** — Flask's existing MQTT client subscribes to `MQTT_TOPIC`; the new subscriber subscribes to `MQTT_STATUS_TOPIC`. If a single `PahoMqttClient` instance subscribes to both topics, the `on_message` callback has to dispatch by topic. → Mitigation: the design extends `PahoMqttClient.__init__` with an optional `status_dispatch_callback` + `status_topic` pair, mirroring the existing `dispatch_callback` + `topic` pair, and the client's `on_connect` subscribes to both topics in a single `client.subscribe([(topic, 1), (status_topic, 1)])` call (paho accepts a list of topic+qos tuples). One client, one network thread, two topic handlers.

## Migration Plan

This is a purely additive change — no existing wire shapes, no existing UI elements, no existing files are removed.

1. **Deploy order:** Ship the Flask side first (subscriber + endpoint + UI), then ship the Pi side (publisher). With the Flask side deployed first, `GET /api/sign-status` returns 204 (no data yet) and the UI shows the grey "unknown" state — operators see "Sign Health: waiting for first status." When the Pi side ships, the UI flips to live.
2. **Adafruit IO feed creation:** the operator must create the new `lindsay-50-status` feed in the AIO dashboard before the Pi's first publish lands. Documented in the `settings.toml.example` for both server and device.
3. **Rollback:** disable the `_status_publisher` `threading.Timer` in `heart-matrix-controller/main.py` (one-line guard), or revert the Flask subscriber changes. The `.status.json` path is untouched and continues to work.
4. **No data migration:** no SQLite rows, no S3 keys, no env-var format changes.

## Open Questions

- **Should `messages_rendered` be a rolling counter or a snapshot?** Current `StatusSnapshot` snapshots it (read of `len(_msgs._msgs)` at write time). The Flask UI will display it as "X messages currently in the buffer." If the operator wants "total messages rendered since boot," that's a different metric that requires a monotonic counter in `main.py`. Not blocking — the snapshot value is the right starting point.

- **Should the dashboard pill state live in `data-state` or in a class?** The design uses `data-state="live|amber|unknown"` plus Tailwind classes toggled by JS. The alternative is to put all CSS in classes (`.sign-pill--live`, `.sign-pill--amber`, `.sign-pill--unknown`) and have JS just toggle the class. The latter is more idiomatic Tailwind; the former is more grep-able. The implementation will pick one — not blocking for this spec.