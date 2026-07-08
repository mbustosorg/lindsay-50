## Context

The Pi already publishes rich runtime state to a local file (`.status.json` at the repo root) every ~3 seconds, via the throttled `StatusWriter` in `heart-matrix-controller/status.py`. That snapshot covers everything the issue asks for and more: `pid`, `active_sha`, `started_at`, `updated_at`, `uptime_seconds`, `mqtt_connected`, `last_tick_age_ms`, `messages_rendered`, `last_error`. The file is consumed **only** by the loader (`heart-matrix-controller/loader.py`) to decide whether to swap a staged worktree. The Flask server has no MQTT topic carrying this state, no subscriber, and no UI surface for it.

The codebase already has a strong precedent for "device → Flask/browser over MQTT" with **no HTTP polling** on the browser side. The existing `MQTT_TOPIC` carries `MessageEnvelope`s (`type` + `payload`); the browser subscribes to that same topic via the MQTT-WS bridge (`static/mqtt_ws_client.js`'s `createMqttWsClient({...topic})`) — there is no `GET /api/live-messages` polling loop for the main envelope flow. The dashboard, testing, and preview pages all get their live data through this single WS bridge.

This change extends that pattern: a second MQTT topic, a second `createMqttWsClient` instance in a new `sign_status.js` module, a small Flask-side in-memory store (`LatestSignStatus`) for load-time hydration, and a `GET /api/sign-status` endpoint that the browser calls **once on page load** (not on a timer). The combination gives the operator two things at once:

- **Live updates while the dashboard is open** — the WS subscription delivers new messages as they arrive, and a local 5s `setInterval` re-evaluates state so the pill transitions `live → unsure → offline` even between messages.
- **Load-time hydration on login** — the `fetch('/api/sign-status')` call returns the latest snapshot Flask has on hand (Flask is subscribed to the same topic, so it has the most recent one), so the operator sees the current state immediately, not "Unknown" until the next 30s heartbeat.

There are also two existing UI patterns that surface a similar "live signal":
- `#mqtt-status` in `templates/base.html:166` — pill driven by `static/mqtt_ws_client.js`, showing the **browser's** WebSocket connection status to the Flask MQTT-WS bridge. This is NOT the same signal as "is the Pi alive" — it's "is the operator's browser talking to the broker."
- The dashboard "Live" pill (`templates/dashboard.html:10-13`) — hardcoded green, no runtime signal.

The new `sign-status-reports` capability sits between these: the Pi publishes a runtime snapshot; Flask subscribes (in-memory store); the browser hydrates on load and subscribes for live; the dashboard pill and the new Settings-page section both render the latest snapshot. The existing `#mqtt-status` is untouched (it's still a separate, useful "browser → broker" signal for the envelope flow).

## Goals / Non-Goals

**Goals:**

- The Pi publishes its existing `StatusSnapshot` over MQTT on a dedicated status topic, throttled to a 30s cadence (separate from the 3s `.status.json` write — the loader's file write cadence is unchanged).
- The **Flask server** subscribes to that status topic and keeps the latest received snapshot in a `threading.RLock`-guarded in-memory store (`LatestSignStatus`).
- Flask exposes `GET /api/sign-status` returning the latest snapshot (or `{snapshot: null}` if none has been received since Flask started). The endpoint always returns 200.
- The **browser** does a single `fetch('/api/sign-status')` on page load (load-time hydration) and subscribes to `MQTT_STATUS_TOPIC` via a second `createMqttWsClient` instance for live updates going forward. The load-time fetch is hydration, NOT polling — the browser does not call `fetch` on a timer.
- The browser computes the sign's state (`live` / `unsure` / `offline`) from the snapshot's age, with thresholds defined as constants in `sign_status.js` (default: `live` < 60s, `unsure` 60-120s, `offline` > 120s or never).
- The browser re-evaluates state every 5 seconds via a local `setInterval` so the pill transitions `live → unsure → offline` even when no new message arrives. The 5s cadence is a UI cadence, not a network cadence — it does not poll the server.
- The Dashboard's static "Live" pill becomes real: green+pulse ("Live") when the snapshot is <60s old, amber+no-pulse ("Live") when 60-120s, grey ("Unknown") when >120s or never.
- The Settings page gets a new read-only **Sign Health** section at the top (above "Sign Name") with the snapshot fields and the timestamp the browser received the snapshot. The fields render only when a snapshot is in memory; the section shows "No status received yet" otherwise.

**Non-Goals:**

- No new runtime health metric — the snapshot is exactly the existing `StatusSnapshot` shape, no new fields are added. If the issue author wants a new metric later (e.g., "free disk space"), that's a follow-up change to the snapshot schema in `status.py`.
- No broker-side persistence — Flask keeps only the latest snapshot in memory. Historical snapshots ("show me the last 24 hours") are out of scope.
- **No HTTP polling.** The browser does not call `fetch` on a timer. The only `setInterval` is local, driven by the last received snapshot's age, and produces only DOM updates.
- No changes to the existing `MessageEnvelope` wire shape or the existing `MQTT_TOPIC` subscribe path.
- No `.status.json` change — the loader's 3s-throttled file write is unchanged; the MQTT publish is a second consumer of the same dataclass.
- No change to the existing `#mqtt-status` browser-WS pill. That signal stays as it is (it tracks the envelope-flow WS, not the status-flow WS).
- **No events/logs topic.** A discrete-event topic (`rebooted`, `upgrading`, `new message received`, etc.) is parked as a separate follow-up change. Reasons: different shape and cadence (discrete appends vs periodic snapshots), different retrieval model (pull-on-demand via AIO REST API vs real-time subscribe — the dashboard pill needs sub-minute freshness; an event log doesn't), and different wire shape (small event dict vs the full snapshot). Adding it to this change would force a third `type` value into the `MessageEnvelope` contract and require every existing envelope consumer to learn to ignore it. The follow-up change adds a third topic (`{MQTT_TOPIC}-events`, default `MQTT_EVENTS_TOPIC`) and a debug-page surface that fetches history from the AIO REST API.
- No new dependencies. `threading.Timer` and `threading.RLock` are stdlib. The browser uses the existing `createMqttWsClient` shim — no new JS libraries.

## Decisions

### 1. Status publish cadence: 30s wall-clock, not 3s like the file write

The 3s `.status.json` cadence is the loader's contract — a fresh file proves the app is alive and rendering, which is what the loader probes every ~8s before swapping. The MQTT publish is the admin UI's contract — a human watching the dashboard doesn't need 3s granularity, and 3s × 24h × N signs of broker writes is wasteful. 30s is a balance: a missed publish is noticeable within ~60s on the dashboard (the green/amber boundary), and the broker load is 1 message per minute per sign.

**Alternative considered:** publish on every render-loop tick (like the `.status.json` writer) and let the broker drop duplicates. Rejected — broker writes are more expensive than local `os.replace`, and the UI doesn't benefit from sub-30s resolution.

**Alternative considered:** publish only on state change (mqtt_connected flips, last_error appears, active_sha changes). Rejected — operators want a "last seen" signal, and silent absence on a healthy sign is ambiguous (is the broker broken, or is the sign actually idle?). A 30s heartbeat is unambiguous: missing heartbeat = unreachable.

### 2. Separate MQTT topic, not a `type="status"` envelope

The existing `MQTT_TOPIC` carries `MessageEnvelope`s with `type` ∈ `{"message", "config"}`. Adding `type="status"` to that schema would force every consumer to handle the new type, including the browser MQTT-WS bridge (which already has its own dispatch logic). A separate topic with a separate handler keeps blast radius small: the envelope consumer doesn't change, the status consumer is a new file, and a misconfigured broker only breaks one flow.

**Alternative considered:** a `type="status"` envelope on the existing topic. Rejected for the blast-radius reason above. The cost of a separate topic (one more broker subscription) is negligible.

**Topic derivation:** `MQTT_STATUS_TOPIC` defaults to `{MQTT_TOPIC}-status` (e.g., `mbustosorg/feeds/lindsay-50-status` on Adafruit IO). Operators can override via `settings.toml` or env var. Adafruit IO requires explicit feed creation — `lindsay-50-status` would need to be created in the AIO dashboard before the first publish lands. This is documented in the new `settings.toml.example` lines for both server and device.

### 3. QoS 0 (fire-and-forget) for status publishes

The render loop on the Pi is the producer; it can't afford to block on a slow broker. The status publish runs in a `threading.Timer` callback (separate from the render loop), but a network stall could still delay the next timer firing. QoS 0 means "best effort" — the publish returns immediately after `client.publish(...)` enqueues the message. A missed status publish is self-healing on the next 30s tick. The Flask subscriber uses QoS 1 (matching the existing `client.subscribe(topic)` convention in `PahoMqttClient`), and the browser-side WS bridge subscribes with QoS 1 (matching the existing `createMqttWsClient` SUBSCRIBE behavior), so neither loses a snapshot because of a transient network blip.

**Alternative considered:** QoS 1 on both sides. Rejected — the producer's QoS 1 contract is "I will retry until PUBACK," which is exactly the blocking behavior we're trying to avoid.

### 4. Flask stores the latest; browser hydrates on load (one-shot, not polling)

The browser is not always open — the operator may be away for hours, and a pure WS subscription would mean the operator sees "Unknown" until the next 30s heartbeat after login. To give the operator the current state immediately, Flask subscribes to the status topic and keeps the latest snapshot in `LatestSignStatus`. The browser does a single `fetch('/api/sign-status')` on page load to read the current snapshot, then transitions to the WS subscription for live updates.

This is a **one-shot fetch on load** — the browser does NOT call `fetch` on a `setInterval`. The fetch is hydration (read-once-on-mount), not polling. The HTTP call is the bridge between "Flask has the latest" and "the browser needs to know what's happening now," used exactly once per page load.

**Alternative considered:** pure browser WS, no Flask subscriber, no fetch. Rejected — fails the "log in and see current state" requirement (operator sees "Unknown" until the next 30s heartbeat).

**Alternative considered:** Flask proxies AIO REST API (`GET https://io.adafruit.com/api/v2/{user}/feeds/{feed}/data/last`) for load-time hydration. Rejected — depends on AIO REST being up, AIO rate limits (1 req/sec free tier), feed retention finite (100 messages by default, decays). The Flask-side MQTT subscription is more robust (Flask keeps a copy; if AIO is briefly flaky, the Flask store still has the latest).

**Alternative considered:** Flask republishes enriched status to a derived topic, browser subscribes to derived. Rejected — doubles the topics, doubles the broker writes, and adds a Flask-side component that has no other purpose.

### 5. Browser-side state computation, not server-side

The threshold policy ("when is missing status a concern?") lives in the browser, not the server. The browser can compute `(now - updated_at)` from the snapshot's `updated_at` field — it doesn't need a server round-trip to know the snapshot's age. The thresholds (60s, 120s) are constants in `sign_status.js`; tuning them is a one-file edit.

**Why not server-side state:** the server's job in this design is to keep a copy of the latest snapshot (so the load-time fetch can return it). The thresholds are UI policy, not security policy — duplicating them in the browser is acceptable, and avoiding a derived topic keeps the architecture clean.

**Threshold defaults:** `live` < 60s, `unsure` 60-120s, `offline` > 120s or never. Hardcoded constants in `sign_status.js` for v1, named so future work can lift them into a config without renaming.

### 6. Browser-side state machine via local `setInterval` (5s)

A pure event-driven browser (no local timer) can't transition `live → unsure → offline` on its own — the only signal is "a new message arrived" or "a load-time fetch returned." A 5s local `setInterval` re-evaluates state from `(now - lastSnapshot.updatedAt)` and re-renders the pill. This is **not** network polling — the interval fires regardless of network state and produces only DOM updates. The cost is one `setInterval` callback per page tab, every 5 seconds — negligible.

**Why 5s:** the amber/offline boundary is 120s, so 5s granularity means a transition is visible within 5s of crossing the threshold. Anything faster is wasteful; anything slower means a stale-looking pill.

### 7. Load-time fetch vs WS subscription: race condition handling

The load-time fetch and the WS subscription both feed the same in-memory snapshot in the browser. There's a potential race:
- Page loads → browser opens WS (in-flight) → browser fires `fetch('/api/sign-status')`
- WS receives a new message (say, at T=5s) before the fetch returns (say, at T=8s)
- The fetch response overwrites the newer snapshot with older data

To prevent regression: the browser compares `fetched.updatedAt` against the in-memory `lastReceivedAt`. If the fetch returns an older timestamp, the response is ignored. The browser's "last write wins by timestamp" rule means a stale load-time response can't overwrite a fresher WS message.

### 8. Second `createMqttWsClient` instance, not an extended shim

The existing `createMqttWsClient({...topic})` shim (`static/mqtt_ws_client.js`) takes a single `topic` parameter. To support a second topic in the same shim, we'd need: (a) a multi-topic subscribe, (b) topic-aware dispatch. The cost of this extension is moderate shim surgery. The alternative — `sign_status.js` creates its own `createMqttWsClient` instance for the status topic — is one extra WebSocket connection (to the same broker, same auth) and zero changes to the shim. The two clients are independent: a status-WS reconnect doesn't affect the envelope-WS, and vice versa.

**Alternative considered:** extend the shim to support multiple topics with a single WS connection. Rejected — the cost (surgery to a security-sensitive WS shim) outweighs the benefit (one fewer WS connection per tab). The shim is the boundary between the page and the broker; making it more flexible for one consumer is the wrong place to absorb the change.

### 9. Fresh paho client per status publish

The existing `PahoMqttClient.publish_envelope` already opens a fresh `mqtt.Client` per call (the docstring explains why — paho's `loop_start` is required for the `wait_for_publish` to resolve, and a long-lived publisher would need careful lifecycle management). The new `publish_status(payload, topic)` follows the same pattern. Status publishes are infrequent (every 30s), so the per-call handshake cost (~50-200ms) is negligible.

**Alternative considered:** long-lived paho publisher on the Pi running in a daemon thread. Rejected — adds a second daemon thread to the Pi's already-threaded process, and `publish_envelope` already established the fresh-client-per-call pattern as the convention.

### 10. The `pid` field is dropped from the wire format

The dataclass keeps `pid` (the OS-level PID of the running app). The Flask UI and the browser don't care which OS PID is rendering — they care about `active_sha`, `started_at`, `uptime_seconds`, `mqtt_connected`, `last_error`. `pid` is host-local and is consumed by the loader for diagnostics, not by Flask or the browser. `StatusSnapshot.to_mqtt_dict()` drops it; `StatusSnapshot.to_dict()` (used for `.status.json`) keeps it.

**Alternative considered:** send everything over the wire and let Flask/browser ignore `pid`. Rejected — explicit drop is the documentation; future readers of the wire shape don't wonder "what's this pid field for?"

## Risks / Trade-offs

- **Stale-snapshot UI lies** — the dashboard pill says "Live" if the snapshot is <60s old. If the Pi freezes but keeps the render loop alive enough to publish (improbable but possible), the UI says green when the sign is actually frozen. → Mitigation: the loader's separate `.status.json` probe still detects a stuck render loop (no fresh file mtime = no swap); the dashboard's "Live" pill is a UI signal, not a health check. If the operator sees green but suspects a problem, the `.status.json` mtime on the Pi is the authoritative signal.

- **Browser-WS outage masks Pi outage** — if the operator's browser loses its WS connection to the broker, the status-WS client stops receiving messages and the pill eventually transitions to `offline` based on the snapshot's age. The operator can't tell whether the sign or their own network is at fault. → Mitigation: the existing `#mqtt-status` pill (browser → broker WS for the envelope flow) is still visible in the header; if BOTH pills go grey simultaneously, the operator's network is the suspect. The status-WS client also surfaces its own connection state to a small status indicator next to the pill, parallel to the existing `#mqtt-status` pattern. Document this in the Sign Health section.

- **Pi render-loop regression under broker load** — even at QoS 0, the fresh-client-per-call status publish opens a TCP connection every 30s. On a constrained Pi 4 this could add up if the broker has a slow handshake. → Mitigation: the existing `publish_envelope` already does the same thing on every inbound SMS (which is rare); 30s is the absolute worst case for status. If broker latency is measured to be a problem, future change can switch to a long-lived publisher.

- **Snapshot schema drift** — if `StatusSnapshot` gains a new field (e.g., `free_disk_mb`), the Flask subscriber and the browser-side decoder will see a field they don't know about. → Mitigation: both `LatestSignStatus.update()` and the browser decoder do a defensive merge (only known keys land in the in-memory store; unknown keys are logged at INFO and dropped). This is the same pattern as `SignConfig.from_dict`'s "ignore unknown keys" approach.

- **Two WebSocket connections per browser tab** — the envelope flow already opens one; the new status flow opens a second. → Mitigation: both connections go to the same broker with the same auth, so the cost is one extra TCP+TLS handshake at page load and one extra keepalive. The two clients' reconnect storms are independent — a broker-side throttle on the status topic doesn't affect the envelope topic, and vice versa. The `base.html` ES-module loader is unaffected (both modules pull from the existing `mqtt_ws_client.js` shim).

- **Browser clock drift** — the state computation depends on `(now - updated_at)`, where `now` is the browser's wall clock. A browser with a wildly wrong clock (e.g., a fresh VM with a 2010 date) would show the wrong state. → Mitigation: this is a UI signal, not a security boundary; the operator looking at the pill is the same operator whose browser clock is wrong. The Settings page's `received_at` field (the wall-clock timestamp the browser received the snapshot) gives a cross-check against the browser's own clock.

- **Flask restart loses the latest snapshot** — if Flask restarts, the in-memory `LatestSignStatus` resets to empty. The browser's load-time fetch will return `snapshot: null`; the operator sees "No status received yet" on the Settings page until the next Pi publish (within 30s) lands. → Mitigation: the WS subscription picks up the next publish within 30s; the load-time fetch is a best-effort hydration, not a durable record. If durable status history is needed later, that's a separate change (e.g., persist to S3 or SQLite).

## Migration Plan

This is a purely additive change — no existing wire shapes, no existing UI elements, no existing files are removed.

1. **Deploy order:** Ship the Flask + browser side first (templates, `sign_status.js`, `APP_CONFIG.mqttStatusTopic` injection, Flask subscriber + endpoint + store), then ship the Pi side (publisher). With the Flask + browser side deployed first, the new pill shows grey ("Unknown") and the new Settings-page section shows "No status received yet" — operators see the new UI in its "waiting" state. When the Pi side ships, the UI flips to live.
2. **Adafruit IO feed creation:** the operator must create the new `lindsay-50-status` feed in the AIO dashboard before the Pi's first publish lands. Documented in the `settings.toml.example` for both server and device.
3. **Rollback:** disable the `_status_publisher` `threading.Timer` in `heart-matrix-controller/main.py` (one-line guard), or revert the browser-side `sign_status.js` change. The `.status.json` path is untouched and continues to work; the WS envelope flow is untouched.
4. **No data migration:** no SQLite rows, no S3 keys, no env-var format changes.

## Open Questions

- **Should `sign_status.js` use a separate `#mqtt-status` indicator, or share the existing one?** The existing `#mqtt-status` pill tracks the envelope-flow WS connection. The new status-flow WS has its own connection state. A single shared indicator would conflate the two (an envelope-WS outage would make the status pill look broken even if the status-WS is fine). The current design adds a small secondary indicator next to the status pill (e.g., a small "WS: connected/reconnecting" text), parallel to but separate from the existing `#mqtt-status`. This is a small UI decision that can be tuned in implementation.

- **Should `messages_rendered` be a rolling counter or a snapshot?** Current `StatusSnapshot` snapshots it (read of `len(_msgs._msgs)` at write time). The Settings page will display it as "X messages currently in the buffer." If the operator wants "total messages rendered since boot," that's a different metric that requires a monotonic counter in `main.py`. Not blocking — the snapshot value is the right starting point.

- **Should the dashboard pill state live in `data-state` or in a class?** The design uses `data-state="live|unsure|offline"` plus Tailwind classes toggled by JS. The alternative is to put all CSS in classes (`.sign-pill--live`, `.sign-pill--unsure`, `.sign-pill--offline`) and have JS just toggle the class. The latter is more idiomatic Tailwind; the former is more grep-able. The implementation will pick one — not blocking for this spec.