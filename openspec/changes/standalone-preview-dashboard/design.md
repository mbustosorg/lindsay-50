## Context

The authenticated Flask UI currently treats the preview, dashboard, messages, testing, and settings as separate pages under one sidebar shell. `templates/base.html` loads `static/app.js`, the MQTT-over-WebSocket shim, sign-status logic, PyScript, and `static/preview/heart-message-manager/app_main.py` on every authenticated page. That PyScript bootstrap constructs app-scoped `MessageManager`, `EffectsCoordinator`, an `IndexedDBEventLog` browser mirror of the Pi's event log, and MQTT objects; `app.js` restores message/config state from a versioned `sessionStorage` cache on each full-page navigation. The `/preview` template then adds the canvas-specific `preview_main.py` and `preview.js` render loop.

That arrangement preserves enough state to make conventional multi-page navigation tolerable, but each navigation still replaces the document, PyScript runtime, MQTT connection, callbacks, and coordinator. It also duplicates operator tools:

- `/` server-renders a recent-20 summary.
- `/testing` provides test injection, Current Config, Active Filters, S3 browsing, and a live in-browser message feed with existing `rest`/`mqtt` source badges.
- `/messages` server-renders all SQLite messages in pages of 50 and owns the existing suppress/unsuppress and media presentation.
- `/preview` owns the sign canvas and browser media overlays.

The target model is simpler: `/` is the long-lived simulator document, and secondary tools open in separate tabs. A dashboard refresh is an intentional runtime replacement and asset upgrade, so message/config state no longer needs to survive navigation.

Constraints:

- The browser runs shared Python through PyScript. Message parsing, filtering, selection, coordination, effects, and models MUST remain Python and reuse `lib_shared/`; native JavaScript is limited to browser I/O, canvas, and DOM shims.
- Browser-shipped copies under `heart-message-manager/static/preview/lib_shared/` must remain synchronized with the canonical `lib_shared/` sources listed in `py-config.toml`.
- The existing message/config REST APIs, MQTT envelope contracts, suppression APIs, S3 admin APIs, physical-sign status flow, and Pi behavior remain authoritative.
- Any changed static JavaScript import must receive the matching `?v=N` cache-buster bump in `templates/base.html`.
- The existing preview CSP is path-scoped to `/preview`; hosting PyScript, WASM, MQTT-WS, and remote media on `/` requires moving that policy to the dashboard route.

## Goals / Non-Goals

**Goals:**

- Make `/` the primary, auto-starting, long-lived simulator dashboard.
- Replace the sidebar with explicit Settings, Testing, and Messages links that open in new tabs and leave the dashboard document intact.
- Give the entire simulated-Pi runtime explicit Start and Stop controls, with Stop then Start creating a fresh generation rather than resuming.
- Remove cross-navigation message/config caching and scope the simulated-Pi bootstrap to the dashboard only.
- Consolidate the preview canvas, recent-100 in-browser message management, test injection, Current Config, Active Filters, and S3 browser into the dashboard.
- Preserve `/messages` as a separate paginated archive of every canonical SQLite message and make its rows ready for future actions.
- Preserve Settings and the transitional Testing page during rollout.
- Keep REST-seeded versus live-MQTT receipt visible using the existing `MessageView.source` contract.

**Non-Goals:**

- Publishing browser-simulator status snapshots to MQTT. The physical sign currently owns the status topic and source-less snapshot shape; a preview-specific topic or source identity is a follow-up.
- Adding permanent message deletion, a delete API, or delete UI. The archive only gains stable row hooks for that future capability.
- Removing `/testing` in this change. Removal follows only after the dashboard replacement is proven.
- Converting the admin UI to an SPA, adding a service worker, or synchronizing multiple dashboard tabs.
- Changing Pi startup behavior, MQTT/REST wire shapes, SQLite/S3 schemas, or Settings semantics.
- Reimplementing shared Python behavior in JavaScript.

## Decisions

### 1. One dashboard document owns one simulator runtime generation

`GET /` becomes the only page that loads the canvas runtime and simulated-Pi message/config MQTT subscriber. `GET /preview` becomes a compatibility redirect to the dashboard preview section. Settings, Testing, and Messages remain normal Flask pages but do not load the simulator bootstrap.

The sidebar is removed from the authenticated shell. The dashboard presents explicit links to Settings, Testing, and Messages using `target="_blank" rel="noopener noreferrer"`; Logout remains in the dashboard chrome and keeps its current behavior. This avoids implying in-place routing and uses ordinary browser tabs rather than introducing an SPA or popup-management layer.

The physical-sign status module remains independent: it may continue to run on the dashboard and Settings where its DOM targets exist, but it is not the browser simulated-Pi message/config runtime.

**Alternative considered:** keep the sidebar and intercept navigation into managed popups. Rejected because it preserves the in-place-navigation affordance the operator explicitly wants removed and adds popup lifecycle complexity.

### 2. A Python dashboard runtime controller owns lifecycle and generation identity

Introduce a browser-specific Python orchestration layer under `heart-message-manager/static/preview/heart-message-manager/` (or refactor `app_main.py` into that role). It is a controller around the canonical shared classes, not a browser port of them. The controller owns one generation record containing:

- a monotonically increasing generation identifier;
- `MessageManager` and its `InMemoryMessages(maxlen=100)` state;
- `EffectsCoordinator`, scroller, canvas binding, and the in-memory selector event log;
- `MqttWsClient` and its Python callback proxies;
- render-loop handle, timers, and registered DOM callbacks;
- lifecycle status: `starting`, `running`, `stopping`, `stopped`, or `error`.

Generation identity is enforced through a single mechanism: every asynchronous callback registered by the runtime is wrapped, at registration time, in a small closure that captures the current generation identifier once and is then handed to the underlying client, scheduler, or DOM API. The wrapper looks roughly like:

```python
def wrap(cb):
    captured_gen = self.generation
    def gated(*args, **kwargs):
        if self.generation != captured_gen:
            return
        cb(*args, **kwargs)
    return gated
```

Registration sites — `mqtt.on_message(...)`, `mqtt.on_connect(...)`, `mqtt.on_disconnect(...)`, `requestAnimationFrame(tick)`, `preview_js.on_render = tick`, `fetch('/api/messages').then(...)`, `setTimeout(...)`, DOM event listeners — always go through `wrap(...)`. Anything not registered this way is not registered at all. The captured `captured_gen` is a closure-local variable bound once at wrap time and cannot drift, be reassigned, or be observed by other code, so there is no question of "did this callback get bound to the right generation?" — the answer is fixed the instant the wrapper is constructed.

Stop invalidates the current generation (assigns a new sentinel to `self.generation`) and then powers the simulated Pi off completely: disconnects the MQTT-over-WebSocket subscription, releases the shared Python `MessageManager` and its in-memory ring, discards the in-memory browser selector event queue, cancels the render loop, releases the coordinator/scroller/canvas binding, releases the registered wrapper proxies, destroys timers, and releases all runtime references. After Stop, the dashboard reports `stopped` and accepts no inbound message or config envelope. Because `self.generation` was already changed before teardown began, callbacks still in flight at the instant of Stop see `self.generation != captured_gen` and early-return without mutating anything. Start allocates a fresh generation identifier, so even a callback that was queued across a Stop-then-Start transition (an in-flight MQTT frame, a pending `fetch` response, a queued `requestAnimationFrame`) fails the `self.generation != captured_gen` check against the new generation and returns without dispatching.

Start is disabled while `starting` or `running`. After Stop, Start allocates a new generation and constructs new shared objects. There is no pause/resume path and no third Reset control: Stop followed by Start is the reset operation.

**Alternative considered:** add only an animation pause while leaving the app-scoped manager and MQTT client alive. Rejected because the chosen semantics are for the whole simulator to stop and start fresh.

### 3. Fresh Start uses REST seed plus a new MQTT subscription and no message/config hydrate

Each new generation:

1. constructs a fresh bounded in-memory browser selector event queue for the new generation;
2. constructs a fresh `MessageManager` backed by its default 100-record `InMemoryMessages`;
3. seeds canonical messages and current config through the existing authenticated `/api/messages` and `/api/config` paths;
4. constructs/connects a fresh MQTT-over-WebSocket subscription for message/config envelopes;
5. binds and starts a fresh coordinator/scroller/canvas stack;
6. starts the requestAnimationFrame loop and marks the generation running.

The exact sequencing must keep the status visible and tear down partially-created resources on failure. MQTT-delivered envelopes continue to call the same shared Python `MessageManager.dispatch()` path used today.

The versioned `sessionStorage` message/config cache and its hydration bootstrap are removed. Refresh and Start intentionally pay the one-shot REST seed cost. The browser selector event log is a bounded in-memory queue (default cap 100 entries, FIFO drop-oldest) implementing the same contract the Pi's JSONL `EventLog` exposes: `append(event)`, `query(event_type, message_id, since)`, `last_for(message_id, event_type)`, immutable rows of `{event_type, message_id, timestamp, received_at}`. The runtime controller constructs a fresh instance for each generation; the prior generation's queue is released during Stop, and a fresh Start creates a new one, so selection recency never turns reset into resume. Removing IndexedDB persistence also removes the quota/private-mode failure mode the prior implementation had to handle.

**Alternative considered:** retain `sessionStorage` as a faster cold-start cache. Rejected because it exists specifically to bridge page navigation, while the new long-lived dashboard eliminates that requirement and intentionally treats refresh as a clean start/upgrade boundary.

### 4. Dashboard-scoped assets and CSP replace every-page bootstrap

`base.html` should provide a lightweight authenticated shell and page blocks. Only the dashboard includes the preview PyScript entrypoint, `preview.js`, simulator APP_CONFIG values, and message/config MQTT shim usage. Secondary pages receive only assets they actually use; the Settings physical-sign status section may still include its dedicated status client.

The existing preview CSP hook is renamed/re-scoped from `/preview` to `/` so the dashboard permits PyScript/CDN/WASM, broker WebSocket, and configured S3/media origins. The redirecting `/preview` response does not need a second runtime policy.

`preview_main.py` and the app runtime must be loaded in a deterministic order rather than relying on two uncoordinated async page tasks. The dashboard controller exposes one ready promise/hook to `preview.js`; Start remains unavailable until that hook resolves.

Any edit to `static/*.js` is accompanied by the required base-template cache-buster increment.

### 5. The dashboard reuses existing Testing and Preview components

The dashboard composes, rather than rewrites, the current interfaces:

- canvas/media overlays and diagnostics bridge from `preview.html`, `preview_main.py`, and `preview.js`;
- test-message form and `/api/test-messages` submission from `testing.html`;
- Current Config and Active Filters data from the running Python `MessageManager` via the existing Python-to-JS getters;
- S3 tree loading through the existing authenticated `/api/admin/s3-objects` and object/media APIs;
- source badges and message row enrichment already exposed by `MessageView` and the Testing feed.

Current Config, Active Filters, and S3 Browser are accessible through modal triggers. They remain in the dashboard DOM and do not navigate, restart, or create a second runtime. Modal behavior must be accessible (focus moves into the dialog, Escape/close works, focus returns to the trigger, and background content is not interactable while open).

Test injection has two observable milestones: the Flask endpoint accepted the request, then the running simulator actually dispatched the resulting MQTT envelope. The first must not be rendered as proof of the second. If the simulator is stopped, injection can still be accepted by Flask but receives no MQTT-live marker from that stopped generation.

### 6. The dashboard recent table is the simulator ring, not the canonical archive

The dashboard calls the existing Python message view with `limit=100` and suppression disabled so both suppressed and visible records appear. `InMemoryMessages` already enforces the 100-record cap; no second persistence or cap layer is added.

The UI paginates that in-memory list at 20 rows per page and refreshes through the existing change-notification bridge. Each row displays:

- sender/body/time and existing media metadata where appropriate;
- suppression state and matching rules;
- the existing `MessageView.source` as an explicit `REST seed` or `MQTT live` badge;
- the valid suppress or unsuppress action.

Suppress/unsuppress uses the existing endpoints via fetch and updates from authoritative returned or re-fetched state without replacing the dashboard document. Pending actions are single-flight per row; failures restore the prior UI state and remain visible.

**Alternative considered:** make the dashboard table query SQLite directly. Rejected because the dashboard table is specifically the simulated Pi's recent in-memory view and source attribution. SQLite belongs to the all-message archive.

### 7. `/messages` remains the server-authoritative all-message archive

The existing `/messages` route remains separate and continues reading every message from SQLite, newest first, in server-controlled pages of 50. It is not capped at 100 and does not instantiate PyScript, the preview coordinator, or the simulated-Pi MQTT subscriber.

The current media presentation and suppress/unsuppress behavior remain. Each rendered row gains `data-msg-id` and `data-received-at` attributes so a future change can add a permanent-delete route and handler without redesigning row identity. No delete affordance or endpoint ships now.

Malformed/out-of-range page input is bounded or handled explicitly rather than causing an unhandled exception.

### 8. Testing is transitional, not a dashboard dependency

`/testing` and its current tools remain available and are linked from the dashboard in a new tab. The dashboard receives its own canonical copies/composition of test injection and diagnostics; it does not reach into the Testing tab or depend on that tab staying open. Once dashboard end-to-end validation is complete in production, a follow-up change may remove Testing and any code no longer shared.

### 9. Browser simulator health is local in this change

The dashboard renders lifecycle/MQTT status for its current generation from local callbacks. It does not publish `StatusSnapshot` records. Publishing the current source-less Pi shape to `MQTT_STATUS_TOPIC` could overwrite physical-sign health, so status publication is deferred until a preview-specific topic or source-aware status model is specified.

### 10. The browser selector event log is an in-memory queue, not IndexedDB

The browser-side selector event log moves from the prior `IndexedDBEventLog` mirror to a small in-memory queue owned by the running generation. It implements the same `EventLog` contract the Pi's JSONL `EventLog` exposes (`append(event)`, `query(event_type, message_id, since)`, `last_for(message_id, event_type)`, immutable rows of `{event_type, message_id, timestamp, received_at}`), and uses a `collections.deque(maxlen=N)` (default cap 100, FIFO drop-oldest) as the backing store. The browser-side module is renamed accordingly and lives next to the dashboard runtime controller; the Pi's JSONL `EventLog` is unchanged.

Rationale:

- The event log's lifetime now matches the running generation. Stop throws it away, refresh throws the document away, and cross-tab sync is out of scope, so there is no cross-navigation survival requirement that IndexedDB would address.
- A `deque(maxlen=N)` removes the quota/private-mode failure mode the IDB mirror had to handle, eliminates per-event IndexedDB write transactions from the hot path, and makes Stop teardown a single reference drop.
- The immutable row schema and the `MessageSelector` consumer contract stay the same, so the selector code does not need a second backend. Only the storage backing changes.

**Alternative considered:** keep the IDB mirror and add a "wipe on Stop" path. Rejected because the in-memory queue is strictly simpler, has no failure modes, and Stop just discards the reference.

## Risks / Trade-offs

- **[Risk] Stop leaves callbacks or reconnect timers alive** → Centralize ownership in the Python runtime controller, invalidate the generation before teardown (assigning a fresh sentinel to `self.generation`), make Stop idempotent, route every async registration through the wrap-once generation-gated closure, and test delayed callbacks against a newer generation.
- **[Risk] Stop does not fully emulate powering the Pi off** → Explicitly disconnect the MQTT subscription, release the shared Python `MessageManager` and its in-memory ring, discard the in-memory browser selector event queue, and verify that no subsequent envelope can mutate a stopped generation.
- **[Risk] PyScript/CSP failures make the new main route appear blank** → Move the existing preview CSP tests to `/`, expose explicit loading/error states, and exercise the dashboard in a real browser before commit.
- **[Risk] Multiple dashboard tabs create independent simulators and MQTT clients** → Accept as the ordinary multi-tab model; each tab labels only what its own generation received. Cross-tab primary election is out of scope.
- **[Risk] Background-tab throttling delays rendering or MQTT recovery** → Surface reconnect/error state and rely on the MQTT client's existing reconnect behavior; a refresh or Stop/Start intentionally creates a clean generation.
- **[Risk] Re-seeding on every Start adds REST load** → The action is operator-controlled and limited to two small canonical requests; remove automatic navigation-triggered seeds from secondary pages.
- **[Risk] Removing the cache exposes seed failures that cached state previously masked** → Keep the simulator stopped/error-visible, report the failing endpoint, clean partial resources, and allow a fresh Start retry.
- **[Risk] Dashboard and an already-open Messages tab show different suppression state** → Each tab updates its own authoritative view; no cross-tab synchronization is promised. Refreshing Messages reads current SQLite/config state.
- **[Trade-off] The dashboard contains both simulation and management UI** → Use clear regions, client pagination, and modal diagnostics so the canvas remains the primary focus without hiding management capabilities.
- **[Trade-off] Testing remains duplicated temporarily** → Accept short-term duplication to preserve the existing diagnostic fallback; document removal as follow-up work.

## Migration Plan

1. Add/refactor the Python dashboard runtime controller and tests for fresh Start, complete Stop, failure cleanup, and generation-discriminated callbacks.
2. Refactor the Flask template shell: remove the sidebar, add dashboard new-tab links, render preview/runtime assets only for `/`, redirect `/preview`, and move the preview CSP to `/`.
3. Build the dashboard layout by composing the existing canvas/media overlay, lifecycle status/controls, test injection, three diagnostic modals, and recent-100 table.
4. Move browser bootstrap ownership from every authenticated page to the dashboard; remove `sessionStorage` message/config caching only after the fresh REST-seed path is covered.
5. Keep `/messages` on SQLite with 50-row server pagination, add stable row data attributes, and retain media and suppression behavior.
6. Keep Settings and Testing functional as standalone new-tab pages; verify neither creates the simulated-Pi runtime.
7. Update static cache-busters and the PyScript file manifest/mirrored `lib_shared` copies for every changed browser asset.
8. Run the full pytest suite and end-to-end browser verification for initial start, Stop/Start reset, MQTT receipt, REST/MQTT badges, suppression actions, modals, S3 browsing, new-tab navigation, refresh reset, `/preview` redirect, and the all-message archive.

No database, S3, message-wire, or config migration is required.

**Rollback:** restore the prior dashboard/preview templates and sidebar, re-enable the every-page bootstrap and `sessionStorage` hydrate path, move CSP back to `/preview`, and remove only the new dashboard composition/controller. The in-memory event-log subclass is reverted to the prior `IndexedDBEventLog` mirror. SQLite/S3 data and existing APIs remain compatible throughout.

## Open Questions

None blocking implementation. Follow-up changes will decide:

- when the transitional Testing page can be removed;
- the permanent-message-deletion contract and audit/backup semantics;
- whether browser status uses a preview-only MQTT topic or a source-aware shared status model.
