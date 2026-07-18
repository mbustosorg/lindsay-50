## Context

The Pi already publishes rich runtime state to a local file (`.status.json` at the repo root) every ~3 seconds, via the throttled `StatusWriter` in `heart-matrix-controller/status.py`. That snapshot covers everything the issue asks for and more: `active_sha`, `short_sha`, `started_at`, `updated_at`, `uptime_seconds` (int), `mqtt_connected`, `last_error`. The file is consumed **only** by the loader (`heart-matrix-controller/loader.py`) to decide whether to swap a staged worktree. The Flask server has no MQTT topic carrying this state, no subscriber, and no UI surface for it.

The codebase already has a strong precedent for "device → Flask/browser over MQTT" with **no HTTP polling** on the browser side. The existing `MQTT_TOPIC` carries `MessageEnvelope`s (`type` + `payload`); the browser subscribes to that same topic via the MQTT-WS bridge (`static/mqtt_ws_client.js`'s `createMqttWsClient({...topic})`) — there is no `GET /api/live-messages` polling loop for the main envelope flow. The dashboard, testing, and preview pages all get their live data through this single WS bridge.

This change extends that pattern: a second MQTT topic, a second `createMqttWsClient` instance in a new `sign_status.js` module, a small Flask-side in-memory store (`LatestSignStatus`) for load-time hydration, and a `GET /api/sign-status` endpoint that the browser calls **once on page load** (not on a timer). The combination gives the operator two things at once:

- **Live updates while the dashboard is open** — the WS subscription delivers new messages as they arrive, and a local 5s `setInterval` re-evaluates state so the pill transitions `live → unknown → offline` even between messages.
- **Load-time hydration on login** — the `fetch('/api/sign-status')` call returns the latest snapshot Flask has on hand (Flask is subscribed to the same topic, so it has the most recent one), so the operator sees the current state immediately, not "Offline" until the next 5s heartbeat.

There are also two existing UI patterns that surface a similar "live signal":
- `#mqtt-status` in `templates/base.html:166` — pill driven by `static/mqtt_ws_client.js`, showing the **browser's** WebSocket connection status to the Flask MQTT-WS bridge. This is NOT the same signal as "is the Pi alive" — it's "is the operator's browser talking to the broker."
- The dashboard "Live" pill (`templates/dashboard.html:10-13`) — hardcoded green, no runtime signal.

The new `sign-status-reports` capability sits between these: the Pi publishes a runtime snapshot; Flask subscribes (in-memory store); the browser hydrates on load and subscribes for live; the dashboard pill and the new Settings-page section both render the latest snapshot. The existing `#mqtt-status` is untouched (it's still a separate, useful "browser → broker" signal for the envelope flow).

## Goals / Non-Goals

**Goals:**

- The Pi publishes its existing `StatusSnapshot` over MQTT on a dedicated status topic, throttled to a 5-second cadence — **unified** with the existing `.status.json` file write, both done in the same `StatusWriter.tick()` method. One throttle constant, one code path, one place to change cadence. The 5s value aligns the loader's hold window (17s, raised from 8s to allow 3 missed writes = 3×5s = 15s of silence + 2s of slack) and the dashboard pill's `live` window (15s = 3 missed publishes) — same scale, same signal.
- The Pi uses a **long-lived paho publisher** (new `StatusPublisher` class) for the MQTT path. paho's `loop_start()` runs a background thread; `client.publish()` is thread-safe and just enqueues into the outgoing buffer. At 5s cadence, a fresh-client-per-publish approach would burn 17,280 TCP+TLS handshakes per day per sign — long-lived is the right pattern for a regular heartbeat.
- The **Flask server** subscribes to that status topic and keeps the latest received snapshot in a `threading.RLock`-guarded in-memory store (`LatestSignStatus`).
- Flask exposes `GET /api/sign-status` returning the latest snapshot (or `{snapshot: null}` if none has been received since Flask started). The endpoint always returns 200.
- The **browser** does a single `fetch('/api/sign-status')` on page load (load-time hydration) and subscribes to `MQTT_STATUS_TOPIC` via a second `createMqttWsClient` instance for live updates going forward. The load-time fetch is hydration, NOT polling — the browser does not call `fetch` on a timer.
- The browser computes the sign's **state** (`live` / `unknown` / `offline`) from the snapshot's age, with thresholds defined as constants in `sign_status.js` (default: `live` < 15s, `unknown` 15-30s, `offline` > 30s or never). The 15/30s thresholds match the 5s publish cadence: 15s catches three consecutive missed publishes (the right "something is wrong" signal); 30s catches six (the right "definitively unreachable" signal).
- The browser also computes the sign's **health** (`healthy` / `degraded`) from the snapshot's contents: `healthy` requires `mqtt_connected === true` AND `last_error` is null/empty; any one failure flips health to `degraded`. Health is computed from the snapshot, not from the message presence — a fresh message that says "I'm broken" is degraded, not live. The earlier design had a third `last_tick_age_ms >= 5000ms` signal, but the `_LAST_TICK_MONOTONIC` bookkeeping was never wired up in `main.py`, so the field always read 0 and acted as a false-negative. The remaining two signals are sufficient: a stuck render loop is visible through `last_error` propagation, and a broker outage is visible through `mqtt_connected`.
- The Dashboard pill renders the **combined** state and health: `state=live AND health=healthy` → green pulse "Live"; `state=live AND health=degraded` → amber "Degraded"; `state=unknown` → amber "Unknown"; `state=offline` → grey "Offline". The four-way rendering means the operator can tell at a glance: is the sign running, and is the sign OK?
- The browser re-evaluates state and health every 5 seconds via a local `setInterval` so the pill transitions `live → unknown → offline` and `healthy → degraded → healthy` even when no new message arrives. The 5s cadence is a UI cadence, not a network cadence — it does not poll the server.
- The Settings page gets a new read-only **Sign Health** section at the top (above "Sign Name") with the snapshot fields and the timestamp the browser received the snapshot. The fields render only when a snapshot is in memory; the section shows "No status received yet" otherwise. When `health=degraded`, the section surfaces the failing health check (e.g., "MQTT disconnected" or "Last error: <message>") above the field table.

**Non-Goals:**

- No new runtime health metric — the snapshot is exactly the existing `StatusSnapshot` shape, no new fields are added. If the issue author wants a new metric later (e.g., "free disk space"), that's a follow-up change to the snapshot schema in `status.py`.
- No broker-side persistence — Flask keeps only the latest snapshot in memory. Historical snapshots ("show me the last 24 hours") are out of scope.
- **No HTTP polling.** The browser does not call `fetch` on a timer. The only `setInterval` is local, driven by the last received snapshot's age, and produces only DOM updates.
- No changes to the existing `MessageEnvelope` wire shape or the existing `MQTT_TOPIC` subscribe path.
- The `.status.json` write cadence moves from 3s to 5s — unified with the MQTT publish cadence. The loader's `BOOT_HOLD_S` moves from 8s to 17s to match the new cadence (3×5s = 15s of writes, plus 2s of slack). The dataclass shrinks to 8 fields (drops `pid`, `messages_rendered`, `last_tick_age_ms`; adds `short_sha`; truncates `uptime_seconds` to int); the existing `to_dict()` is the single serializer for both file and wire.
- No change to the existing `#mqtt-status` browser-WS pill. That signal stays as it is (it tracks the envelope-flow WS, not the status-flow WS).
- **No events/logs topic.** A discrete-event topic (`rebooted`, `upgrading`, `new message received`, etc.) is parked as a separate follow-up change. Reasons: different shape and cadence (discrete appends vs periodic snapshots), different retrieval model (pull-on-demand via AIO REST API vs real-time subscribe — the dashboard pill needs sub-minute freshness; an event log doesn't), and different wire shape (small event dict vs the full snapshot). Adding it to this change would force a third `type` value into the `MessageEnvelope` contract and require every existing envelope consumer to learn to ignore it. The follow-up change adds a third topic (`{MQTT_TOPIC}-events`, default `MQTT_EVENTS_TOPIC`) and a debug-page surface that fetches history from the AIO REST API.
- No new dependencies. `threading.Timer` and `threading.RLock` are stdlib. The browser uses the existing `createMqttWsClient` shim — no new JS libraries.

## Decisions

### 1. Status publish cadence: 5s wall-clock, unified with the `.status.json` write

The Pi's existing `StatusWriter` already throttles its `.status.json` file write to 3 seconds. Adding the MQTT publish as a second consumer in the same `tick()` method, at the same 5-second cadence, gives us:

- **One throttle constant.** `STATUS_WRITE_INTERVAL_S = 5.0` controls both the file write and the MQTT publish. To change cadence, one edit.
- **One code path.** `StatusWriter.tick()` writes the file (existing) and publishes the MQTT envelope (new) in the same call. The existing throttling logic (last-write timestamp + conditional update) is reused as-is.
- **Aligned with the loader's hold window.** The loader's `BOOT_HOLD_S` moves to 17s, which gives 3 missed 5s writes (15s) plus 2s of slack before failing. The 5s write cadence is what makes "3 missed writes" the right threshold: at 5s, 3 missed writes = 15s of silence, which matches the dashboard pill's `live` window. The loader, the file mtime, and the UI all read the same signal at the same scale.
- **Headroom for the broker.** Adafruit IO's free tier is 30 publishes/minute (1 every 2s). At 5s, we're at 1/6 of the limit.
- **Tighter UI feedback.** With a 5s heartbeat, the operator sees state changes within 5s, not 30s. The dashboard pill's `live` window can be 15s (3 missed publishes — the "something is wrong" signal) and the `unknown` window 15s more (3 more — the "definitively unreachable" signal), all under the 30s total detection budget.

**Alternative considered:** publish at 30s (a slower cadence tuned for "human doesn't need 3s granularity"). Rejected — 30s creates two cadences (file = 3s, MQTT = 30s), two code paths, and an asymmetric 30s "hydration delay" after a Flask restart. The unification at 5s is simpler; the operator's UI feedback is faster; the broker load (12 publishes/min/sign) is comfortably under the 30/min limit.

**Alternative considered:** publish on every render-loop tick (60Hz) and let the broker drop duplicates. Rejected — 60Hz × 24h × N signs of broker writes is unsustainable, and the UI doesn't benefit from sub-5s resolution.

**Alternative considered:** publish only on state change (mqtt_connected flips, last_error appears, active_sha changes). Rejected — operators want a "last seen" signal, and silent absence on a healthy sign is ambiguous (is the broker broken, or is the sign actually idle?). A 5s heartbeat is unambiguous: missing heartbeat = unreachable.

### 2. Separate MQTT topic, not a `type="status"` envelope

The existing `MQTT_TOPIC` carries `MessageEnvelope`s with `type` ∈ `{"message", "config"}`. Adding `type="status"` to that schema would force every consumer to handle the new type, including the browser MQTT-WS bridge (which already has its own dispatch logic). A separate topic with a separate handler keeps blast radius small: the envelope consumer doesn't change, the status consumer is a new file, and a misconfigured broker only breaks one flow.

**Alternative considered:** a `type="status"` envelope on the existing topic. Rejected for the blast-radius reason above. The cost of a separate topic (one more broker subscription) is negligible.

**Topic derivation:** `MQTT_STATUS_TOPIC` defaults to `{MQTT_TOPIC}-status` (e.g., `mbustosorg/feeds/lindsay-50-status` on Adafruit IO). Operators can override via `settings.toml` or env var. Adafruit IO requires explicit feed creation — `lindsay-50-status` would need to be created in the AIO dashboard before the first publish lands. This is documented in the new `settings.toml.example` lines for both server and device.

### 3. QoS 0 (fire-and-forget) for status publishes

The render loop on the Pi is the producer; it can't afford to block on a slow broker. The status publish runs in the same `tick()` as the `.status.json` file write, so a slow broker publish would add latency to the next render tick. QoS 0 means "best effort" — the long-lived publisher's `client.publish(...)` returns immediately after enqueueing the message; the network is handled by paho's `loop_start()` background thread. A missed status publish is self-healing on the next 5s tick. The Flask subscriber uses QoS 1 (matching the existing `client.subscribe(topic)` convention in `PahoMqttClient`), and the browser-side WS bridge subscribes with QoS 1 (matching the existing `createMqttWsClient` SUBSCRIBE behavior), so neither loses a snapshot because of a transient network blip.

**Alternative considered:** QoS 1 on both sides. Rejected — the producer's QoS 1 contract is "I will retry until PUBACK," which is exactly the blocking behavior we're trying to avoid.

### 4. Flask stores the latest; browser hydrates on load (one-shot, not polling)

The browser is not always open — the operator may be away for hours, and a pure WS subscription would mean the operator sees "Offline" until the next 5s heartbeat after login. To give the operator the current state immediately, Flask subscribes to the status topic and keeps the latest snapshot in `LatestSignStatus`. The browser does a single `fetch('/api/sign-status')` on page load to read the current snapshot, then transitions to the WS subscription for live updates.

This is a **one-shot fetch on load** — the browser does NOT call `fetch` on a `setInterval`. The fetch is hydration (read-once-on-mount), not polling. The HTTP call is the bridge between "Flask has the latest" and "the browser needs to know what's happening now," used exactly once per page load.

**Alternative considered:** pure browser WS, no Flask subscriber, no fetch. Rejected — fails the "log in and see current state" requirement (operator sees "Offline" until the next 5s heartbeat).

**Alternative considered:** Flask proxies AIO REST API (`GET https://io.adafruit.com/api/v2/{user}/feeds/{feed}/data/last`) for load-time hydration. Rejected — depends on AIO REST being up, AIO rate limits (1 req/sec free tier), feed retention finite (100 messages by default, decays). The Flask-side MQTT subscription is more robust (Flask keeps a copy; if AIO is briefly flaky, the Flask store still has the latest).

**Alternative considered:** Flask republishes enriched status to a derived topic, browser subscribes to derived. Rejected — doubles the topics, doubles the broker writes, and adds a Flask-side component that has no other purpose.

### 5. Browser-side state AND health computation, not server-side

The browser computes two orthogonal signals about the sign: **state** (the freshness of the most recent message) and **health** (whether the snapshot's contents say the sign is OK). Both live in the browser, not the server — the browser already has the full snapshot and can compute both from the data it has, with no server round-trip.

**State (freshness):** `stateFromAge(ageSeconds) -> "live" | "unknown" | "offline"` from `(now - updated_at)`. Thresholds: `live` < 15s, `unknown` 15-30s, `offline` > 30s or never. The 15/30s split matches the 5s publish cadence: 15s = 3 missed publishes ("something is wrong"), 30s = 6 missed publishes ("definitively unreachable").

**Health (snapshot contents):** `healthFromSnapshot(snapshot) -> "healthy" | "degraded"`. `degraded` if ANY of: `mqtt_connected === false`, `last_error` is a non-empty string. `healthy` otherwise. The earlier design had a third `last_tick_age_ms >= HEALTH_TICK_AGE_MAX_MS` (5s) signal — that was dropped from the snapshot because the `_LAST_TICK_MONOTONIC` global in `main.py` was never reassigned (it stayed at `0.0`), so the field always read 0 and the threshold was vacuous. The two remaining signals are sufficient: a stuck render loop is visible through `last_error` propagation (the app's exception handler sets it on any caught error), and a broker outage is visible through `mqtt_connected` (the paho client's `is_connected()` returns false).

**Combined render state:** `{state, health} -> renderKey` where renderKey is one of `live-healthy | live-degraded | unknown | offline`. The pill renders each as a distinct text and color (see Decision 11).

**Why browser-side, not server-side:** the server's job in this design is to keep a copy of the latest snapshot (so the load-time fetch can return it). State and health are UI policy, not security policy — duplicating them in the browser is acceptable, and avoiding a derived "computed status" topic keeps the architecture clean. The server exposes the raw snapshot; the browser interprets it.

### 6. Browser-side state machine via local `setInterval` (5s)

A pure event-driven browser (no local timer) can't transition `live → unknown → offline` or `healthy → degraded → healthy` on its own — the only signal is "a new message arrived" or "a load-time fetch returned." A 5s local `setInterval` re-evaluates state and health from the in-memory snapshot and re-renders the pill. This is **not** network polling — the interval fires regardless of network state and produces only DOM updates. The cost is one `setInterval` callback per page tab, every 5 seconds — negligible.

**Why 5s:** the `unknown`/`offline` boundary is 30s, so 5s granularity means a transition is visible within 5s of crossing the threshold. The interval matches the publish cadence — at 5s heartbeat + 5s re-render, the pill is essentially event-driven in normal operation (one re-render per message arrival) and the setInterval is the safety net for when the publish stream dies. Anything faster is wasteful; anything slower means a stale-looking pill.

### 7. Load-time fetch vs WS subscription: race condition handling

The load-time fetch and the WS subscription both feed the same in-memory snapshot in the browser. There's a potential race:
- Page loads → browser opens WS (in-flight) → browser fires `fetch('/api/sign-status')`
- WS receives a new message (say, at T=5s) before the fetch returns (say, at T=8s)
- The fetch response overwrites the newer snapshot with older data

To prevent regression: the browser compares `fetched.updatedAt` against the in-memory `lastReceivedAt`. If the fetch returns an older timestamp, the response is ignored. The browser's "last write wins by timestamp" rule means a stale load-time response can't overwrite a fresher WS message.

### 8. Second `createMqttWsClient` instance, not an extended shim

The existing `createMqttWsClient({...topic})` shim (`static/mqtt_ws_client.js`) takes a single `topic` parameter. To support a second topic in the same shim, we'd need: (a) a multi-topic subscribe, (b) topic-aware dispatch. The cost of this extension is moderate shim surgery. The alternative — `sign_status.js` creates its own `createMqttWsClient` instance for the status topic — is one extra WebSocket connection (to the same broker, same auth) and zero changes to the shim. The two clients are independent: a status-WS reconnect doesn't affect the envelope-WS, and vice versa.

**Alternative considered:** extend the shim to support multiple topics with a single WS connection. Rejected — the cost (surgery to a security-sensitive WS shim) outweighs the benefit (one fewer WS connection per tab). The shim is the boundary between the page and the broker; making it more flexible for one consumer is the wrong place to absorb the change.

### 9. Long-lived paho publisher for status (StatusPublisher class)

The Pi holds a single `mqtt.Client` open for the duration of the render loop. The `StatusPublisher` class wraps this: the constructor calls `connect_async(...)` + `loop_start()`; `publish(payload_dict)` calls `client.publish(topic, payload.encode(), qos=0)` (thread-safe, non-blocking — the loop thread handles the network); `close()` calls `loop_stop()` + `disconnect()`. At 5s cadence, the per-publish handshake cost of a fresh-client-per-call approach (17,280 TCP+TLS handshakes per day per sign) is meaningful; a long-lived publisher eliminates that overhead.

**Why long-lived for status but fresh-client-per-call for envelope:** envelope publishes are driven by irregular inbound SMS events — a few per day at most, with bursts possible. The handshake-per-publish cost is negligible for that volume, and the fresh-client-per-call pattern keeps the envelope path self-contained (no shared state with the status flow, no daemon thread to manage on the Flask side). Status publishes are regular (every 5s) — a long-lived publisher is the right pattern for a regular heartbeat.

**Lifecycle:** the `StatusPublisher` is instantiated at Pi startup, held by the main render-loop process, and `close()`d on shutdown. paho's `loop_start()` runs a background thread that handles the network; the render loop's `tick()` calls `publish()` from its own thread. paho's `client.publish()` is thread-safe (enqueues into the outgoing buffer; the loop thread reads from it). On a `publish()` returning a non-success `rc` (broker disconnect), a `threading.Timer` schedules a reconnect attempt 5s later.

**Alternative considered:** fresh-client-per-call for status (matching the envelope pattern). Rejected — at 5s cadence, the per-publish handshake is the dominant cost. The lifecycle-management complexity that makes fresh-client-per-call attractive for envelope is irrelevant for status (no concurrency between producers, no bursty load, no per-publish auth refresh).

### 10. Reshape the snapshot: drop three dead fields, add `short_sha`, truncate `uptime_seconds`

The `StatusSnapshot` dataclass gets four changes in this iteration:

- **Drop `pid`** (OS process ID): the loader's `read_status` checked for its *presence* as a required key, but `_is_status_healthy` never read the value, and nothing else in the codebase did either. Host-local diagnostic data with no UI use.
- **Drop `messages_rendered`** (rolling buffer length at write time): the snapshot's own dataclass field, never read by the loader (not a required key, not in `_is_status_healthy`), never sent over the wire in the original spec, and the proposed Settings-page display was diagnostic noise. The deque-length read in `main.py:_build_status_snapshot` was a 5-line excursion into the `InMemoryMessages` private internals — `getattr(msgs, "_msgs", None)` to peek at the deque — for a value no one acted on.
- **Drop `last_tick_age_ms`**: the `_LAST_TICK_MONOTONIC` global in `main.py` is initialized to `0.0` on module load and never reassigned anywhere in the codebase, so the snapshot's `last_tick_age_ms` always reads 0 (the `if last_tick_monotonic` check in the snapshot builder is falsy on `0.0`, so the field falls through to the default). The field appeared to be a real health signal — `_is_status_healthy` checked `last_tick_age_ms > 2000` — but it was vacuous. The render-loop's actual health is visible through `last_error` propagation when the app's exception handler catches a render failure, so the third signal was redundant.
- **Add `short_sha`** (first 7 chars of `active_sha`): the Settings page already shows the deployed SHA on Flask's boot-config card (the v2 work added `deployed_sha_short` to the template), so the operator is used to seeing the short form. Adding it to the snapshot is one more string per publish and lets the browser show "running v-b5e191c" inline in the pill text without re-deriving it from `active_sha`. The derivation function lives in `lib_shared.boot_config.short_sha` and is called once at write time; the consumer never recomputes.
- **Truncate `uptime_seconds` to int**: the field is `float` (e.g., `90061.42`) for a value that the UI formats as `Xd Yh Zm` — the fractional part is noise in a 5s heartbeat and forces every consumer to think about float formatting. `int(now_monotonic - _STARTED_AT_MONOTONIC)` is the new write site; `to_dict()` returns it as a JSON integer.

**The asymmetry problem.** The Round 2 design introduced a `to_mqtt_dict()` serializer that dropped `pid` from the wire but kept it in `.status.json`. That was a band-aid: it required two code paths to stay in sync (`.status.json` for the loader, MQTT for Flask), a per-field keep/drop list, and it left `messages_rendered` and `last_tick_age_ms` on the wire anyway. Asymmetric serializers rot.

**The fix: one field set everywhere.** All four changes apply to both the `.status.json` file write and the MQTT wire payload — both use the same `to_dict()` serializer. The loader's `read_status` required-keys list drops `pid` (the value was never read; the presence check was a code smell). The loader's `_is_status_healthy` drops the `last_tick_age_ms` check (the field is gone from the snapshot). The final field set is 8 keys: `schema_version`, `active_sha`, `short_sha`, `started_at`, `updated_at`, `uptime_seconds`, `mqtt_connected`, `last_error`.

**Why drop instead of "send everything and let the UI ignore":** the wire shape is the documentation. A future reader of the broker payload asks "what's `pid` for?" and has to read the code to find out it was a band-aid. An explicit "no `pid`" in the spec is unambiguous. Same for `messages_rendered` and `last_tick_age_ms` — both are numbers with no action; the operator's health-check question is answered by `mqtt_connected` and `last_error` alone.

**Why add `short_sha` instead of letting the browser derive it from `active_sha`:** the browser could call `s.slice(0, 7)` on `active_sha` at render time. But the operator's UI now shows short SHAs in three places (boot-config card, settings sign-health section, future inline pill text) — having the derivation in one place (`lib_shared.boot_config.short_sha`) means the empty-SHA handling, the short-SHA idempotency, and the "first 7 chars" decision live in one function, not in three template-side strings. The cost is one extra string field per publish, which is trivial.

### 11. Health-aware pill: state × health, four render states

The pill's render state is the product of two independent signals: `state` (freshness) and `health` (snapshot contents). The four-cell matrix:

| state \ health | healthy                          | degraded                                    |
| -------------- | -------------------------------- | ------------------------------------------- |
| **live**       | green pulse, "Live"              | amber, "Degraded" (with ⚠ in Settings)      |
| **unknown**    | amber, "Unknown"                 | amber, "Unknown — Degraded"                 |
| **offline**    | grey, "Offline"                  | grey, "Offline" (state dominates; health shown in Settings) |

**Why four render states, not three:** the user's feedback pointed out that "we shouldn't just key off the presence of a message, but whether the contents say the sign is healthy." A sign that says "I'm running, but `mqtt_connected: false` and `last_error: 'broker disconnected'`" is NOT "Live" in the operator's sense — it's running but broken. Collapsing that to a green pulse would make the pill a liar, exactly the failure mode the original 60/120s design was open to.

**Why the `unknown` × `degraded` row is rendered as just "Unknown — Degraded" instead of a separate color:** in practice, an unknown message is already a degraded state (the message stream is unhealthy). The pill doesn't need a third color; the Settings page's Sign Health section surfaces the failing health check below the field table. The pill is a glance; the Settings page is the investigation surface.

**Why `offline` × `degraded` is just "Offline":** when the message stream is dead, the snapshot's health fields are stale (they reflect the moment the last message was sent). Surfacing "offline" with a stale health field is misleading; the operator's next action is the same regardless of the stale health. The Settings page still shows the snapshot's last-known health fields for post-mortem.

**Why `health` lives in `sign_status.js`, not in the Flask endpoint:** the snapshot is the source of truth, and the browser already has the full snapshot. Putting health in the server would require either (a) a derived computed-status topic or (b) a derived computed-status field in the `/api/sign-status` response. Both add surface area. The browser's interpretation is one function call away from the snapshot, with named constants that are easy to find and tune.

**Alternative considered:** collapse `live-degraded` to a green "Live" pill with a small ⚠ icon next to the text. Rejected — a green "Live" pill that means "I'm broken" is a worse failure mode than a clearly-amber "Degraded" pill. The visual signal should match the semantic.

**Alternative considered:** add a separate "Health" pill in the header, distinct from the "Live" pill. Rejected — two pills next to each other is more visual noise than the operator needs. One pill, four states, clear color/text per state.

**Threshold constant:** none. The earlier design had `HEALTH_TICK_AGE_MAX_MS = 5000`, but the `last_tick_age_ms` field was dropped from the snapshot (Decision 10) because the bookkeeping that would have produced a real value was never wired up. The health function now keys off two signals only — `mqtt_connected` and `last_error` — both of which are authoritative (they reflect the live paho client state and the most recent caught exception, respectively).

## Risks / Trade-offs

- **Stale-snapshot UI lies (mitigated by health-aware pill)** — the previous design said the pill was a "freshness signal, not a health check," and pointed operators at the loader's `.status.json` probe for health. That separation was wrong: the snapshot already contains health signals (`mqtt_connected`, `last_error`), and the operator's mental model is "is the sign OK," not "is the message stream fresh." → Mitigation: the new health-aware pill (Decision 11) checks the snapshot's contents. A fresh message that says `mqtt_connected: false` or `last_error: "broker disconnected"` renders the pill as "Degraded" (amber), not "Live" (green). The Settings page's Sign Health section surfaces the failing check below the field table so the operator can drill in. The loader's `.status.json` probe is still there for staged-worktree swaps, but it's no longer the source of truth for the operator's health-check question — the pill is. (The earlier `last_tick_age_ms` health signal was dropped from the snapshot — see Decision 10 — because the bookkeeping that would have produced a real value was never wired up.)

- **Fresh-but-unhealthy sign** — the operator may see "Degraded" for an extended period (e.g., a transient broker outage that takes a few minutes to recover). → Mitigation: the "Degraded" text and amber color are distinct from "Unknown" (also amber, different text), so the operator can tell at a glance that the sign is *running but broken* vs *unreachable*. The Settings page surfaces the specific failing check (e.g., "MQTT disconnected: <last_error>") so the operator can act.

- **Browser-WS outage masks Pi outage** — if the operator's browser loses its WS connection to the broker, the status-WS client stops receiving messages and the pill eventually transitions to `offline` based on the snapshot's age. The operator can't tell whether the sign or their own network is at fault. → Mitigation: the existing `#mqtt-status` pill (browser → broker WS for the envelope flow) is still visible in the header; if BOTH pills go grey simultaneously, the operator's network is the suspect. The status-WS client also surfaces its own connection state to a small status indicator next to the pill, parallel to the existing `#mqtt-status` pattern. Document this in the Sign Health section.

- **Pi render-loop regression under broker load** — at QoS 0, the long-lived paho publisher's `client.publish()` is non-blocking and enqueues into the outgoing buffer; the loop thread handles the network independently. A broker stall or a slow handshake on initial connect does not stall the render loop. The 5s heartbeat is 1/6 of Adafruit IO's free-tier publish limit, so steady-state broker load is comfortable. → Mitigation: if broker latency is measured to be a problem in production, the `StatusPublisher` reconnect timer (5s) bounds the recovery time; a manual `systemctl restart lindsay_50` is the nuclear option.

- **Long-lived publisher lifecycle** — the `StatusPublisher` holds a single `mqtt.Client` open for the lifetime of the Pi process. A bug in `close()` on shutdown could leak the connection (broker-side, until the keepalive expires — typically 60s). → Mitigation: `StatusPublisher.close()` is called from `main.py`'s shutdown path (the existing `try/finally` around the render loop), and the systemd unit has `Restart=on-failure` so a process crash doesn't leak. paho's `disconnect()` sends a clean DISCONNECT packet; the broker drops the connection immediately.

- **Snapshot schema drift** — if `StatusSnapshot` gains a new field (e.g., `free_disk_mb`), the Flask subscriber and the browser-side decoder will see a field they don't know about. → Mitigation: both `LatestSignStatus.update()` and the browser decoder do a defensive merge (only known keys land in the in-memory store; unknown keys are logged at INFO and dropped). This is the same pattern as `SignConfig.from_dict`'s "ignore unknown keys" approach.

- **Two WebSocket connections per browser tab** — the envelope flow already opens one; the new status flow opens a second. → Mitigation: both connections go to the same broker with the same auth, so the cost is one extra TCP+TLS handshake at page load and one extra keepalive. The two clients' reconnect storms are independent — a broker-side throttle on the status topic doesn't affect the envelope topic, and vice versa. The `base.html` ES-module loader is unaffected (both modules pull from the existing `mqtt_ws_client.js` shim).

- **Browser clock drift** — the state computation depends on `(now - updated_at)`, where `now` is the browser's wall clock. A browser with a wildly wrong clock (e.g., a fresh VM with a 2010 date) would show the wrong state. → Mitigation: this is a UI signal, not a security boundary; the operator looking at the pill is the same operator whose browser clock is wrong. The Settings page's `received_at` field (the wall-clock timestamp the browser received the snapshot) gives a cross-check against the browser's own clock.

- **Flask restart loses the latest snapshot** — if Flask restarts, the in-memory `LatestSignStatus` resets to empty. The browser's load-time fetch will return `snapshot: null`; the operator sees "No status received yet" on the Settings page until the next Pi publish (within 5s) lands. → Mitigation: the WS subscription picks up the next publish within 5s; the load-time fetch is a best-effort hydration, not a durable record. The 5s re-hydration time is operationally invisible. If durable status history is needed later, that's a separate change (e.g., persist to S3 or SQLite).

## Migration Plan

This is a mostly additive change — no existing wire shapes, no existing UI elements, no existing files are removed. The behavior changes for existing code paths are: (a) `StatusWriter` cadence moves from 3s to 5s (unified with the new MQTT publish); (b) the loader's `BOOT_HOLD_S` moves from 8s to 17s to match the new cadence (3×5s = 15s of writes, plus 2s of slack). Both are config-level changes with no wire or UI impact.

1. **Deploy order:** Ship the Flask + browser side first (templates, `sign_status.js`, `APP_CONFIG.mqttStatusTopic` injection, Flask subscriber + endpoint + store), then ship the Pi side (StatusPublisher + StatusWriter change + loader BOOT_HOLD_S change). With the Flask + browser side deployed first, the new pill shows grey ("Offline") and the new Settings-page section shows "No status received yet" — operators see the new UI in its "waiting" state. When the Pi side ships, the UI flips to live.
2. **Adafruit IO feed creation:** the operator must create the new `lindsay-50-status` feed in the AIO dashboard before the Pi's first publish lands. Documented in the `settings.toml.example` for both server and device.
3. **Rollback:** disable the MQTT publish call in `StatusWriter.tick()` (one-line guard), or revert the browser-side `sign_status.js` change. The `.status.json` path stays at its new 5s cadence and the loader's `BOOT_HOLD_S` stays at its new 17s — both are still consistent (17s hold + 5s writes = 3 missed writes = 15s of silence + 2s of slack). The WS envelope flow is untouched.
4. **No data migration:** no SQLite rows, no S3 keys, no env-var format changes.

## Open Questions

- **Should `sign_status.js` use a separate `#mqtt-status` indicator, or share the existing one?** The existing `#mqtt-status` pill tracks the envelope-flow WS connection. The new status-flow WS has its own connection state. A single shared indicator would conflate the two (an envelope-WS outage would make the status pill look broken even if the status-WS is fine). The current design adds a small secondary indicator next to the status pill (e.g., a small "WS: connected/reconnecting" text), parallel to but separate from the existing `#mqtt-status`. This is a small UI decision that can be tuned in implementation.

- **Should the dashboard pill state live in `data-state` or in a class?** The design uses `data-state="live-healthy|live-degraded|unknown|offline"` plus Tailwind classes toggled by JS. The alternative is to put all CSS in classes (`.sign-pill--live-healthy`, `.sign-pill--live-degraded`, `.sign-pill--unknown`, `.sign-pill--offline`) and have JS just toggle the class. The latter is more idiomatic Tailwind; the former is more grep-able. The implementation will pick one — not blocking for this spec.