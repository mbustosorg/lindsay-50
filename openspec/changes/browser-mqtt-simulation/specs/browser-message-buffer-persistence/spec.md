## ADDED Requirements

### Requirement: Ring buffer and SignConfig are persisted to browser-side storage

The browser-side `MessageBufferStore` SHALL persist the in-browser `MessageManager`'s state — the ring buffer (most recent 100 messages) and the live `SignConfig` — to IndexedDB. The store SHALL be a small native-JS shim called from the base template's `app.js` (and from PyScript via `create_proxy` for the `/preview` page). Persistence is automatic: every successful `dispatch()` that adds a message or updates the config SHALL write through to IndexedDB. The in-memory state SHALL be the source of truth; the IndexedDB write is a write-through cache.

The IndexedDB database SHALL be named `lindsay-50-browser` with two object stores:

- `messages` — keyPath `id` (the message UUID from the broker), with an index on `received_at` (descending) for hydration
- `config` — keyPath `key` (single record with `key: "current"`) holding the serialized `SignConfig` dict

#### Scenario: A new message is written to IndexedDB after dispatch

- **WHEN** `MessageManager.dispatch(raw)` accepts a `type: "message"` envelope
- **THEN** the new `Message` SHALL be written to the `messages` object store, keyed by `msg.id`; the ring buffer's eviction (if any) SHALL be reflected in the store (the evicted message's `id` is removed)

#### Scenario: A config update is written to IndexedDB after dispatch

- **WHEN** `MessageManager.dispatch(raw)` accepts a `type: "config"` envelope
- **THEN** the updated `SignConfig` dict SHALL be written to the `config` object store under the `current` key, replacing any previous value

#### Scenario: A filtered (suppressed) message is still persisted

- **WHEN** `MessageManager.dispatch(raw)` accepts a `type: "message"` envelope that the filter rules mark as suppressed
- **THEN** the message SHALL still be written to the `messages` object store with `suppressed=True`; the suppression metadata is preserved across reloads

### Requirement: Wipe + re-seed on app start, login, and on the `paused` → `connected` transition

The browser's bootstrap SHALL perform a **wipe + re-seed** of the IndexedDB on three triggers:

1. **App start** (the first page load of an authenticated session, identified by the absence of a session-marker flag in `localStorage`): the IndexedDB `messages` and `config` stores SHALL be cleared; the ring and `SignConfig` SHALL be repopulated from `/api/messages` and `/api/config` (X-API-Key auth, same as the device); the WS connection SHALL then open and append new envelopes.
2. **Login**: when the operator logs in, the IndexedDB SHALL be wiped (any data from a prior session is gone) and re-seeded.
3. **`paused` → `connected` transition**: when the WS client emits a `paused` status event (because the disconnect duration exceeded the configured threshold, default 5 minutes) and subsequently reconnects, the `connected` event SHALL carry `wasLongDisconnect: true`. The base template's `app.js` SHALL listen for this flag and SHALL trigger the wipe + re-seed sequence before the new envelopes are dispatched. The wipe + re-seed is event-driven; the threshold check is the WS client's responsibility, not the bootstrap's.

Wipe + re-seed is the right shape because the broker does not keep a long-term message log (Adafruit IO has no retained messages on a regular topic; local Paho typically doesn't either). The Flask process does (SQLite + S3), and `/api/messages` returns the canonical list. Wipe + re-seed at the trigger points guarantees the browser's IndexedDB matches the canonical list at the start of a session and after any disconnect long enough that some envelopes may have been missed.

#### Scenario: App start wipes and re-seeds the IndexedDB

- **WHEN** the operator loads the first admin page of a new session (no session-marker flag in `localStorage`)
- **THEN** the base template's `app.js` SHALL clear the `messages` and `config` object stores in IndexedDB; SHALL `await mgr.seed()` (where `mgr` is a `MessageManager(..., is_browser=True, ...)` constructed in PyScript — its internal `_fetch` issues the X-API-Key-authed `js.fetch` calls against `/api/messages` and `/api/config` and populates the ring and `SignConfig`); the session-marker flag SHALL be set in `localStorage` to mark the session started

#### Scenario: Login wipes and re-seeds the IndexedDB

- **WHEN** the operator logs in (the auth blueprint's login view redirects after a successful authentication)
- **THEN** the base template's `app.js` SHALL detect the new session (or the auth blueprint SHALL signal it via a `data-wipe-on-load` attribute on the body); the wipe + re-seed sequence SHALL run as in the app-start scenario

#### Scenario: paused → connected triggers wipe and re-seed

- **WHEN** the WS client emits a `paused` status event and subsequently reconnects with `wasLongDisconnect: true`
- **THEN** the base template's `app.js` SHALL clear the IndexedDB `messages` and `config` stores, re-seed from `/api/messages` and `/api/config` (X-API-Key auth, same as the device), and resume envelope dispatch; subsequent envelopes SHALL be appended to the freshly-seeded ring

#### Scenario: Short disconnect does not transition to paused and does not trigger a wipe

- **WHEN** the WS connection drops for a duration shorter than the threshold (e.g. 30 seconds) and reconnects
- **THEN** the WS client SHALL NOT emit `paused`; the `connected` event SHALL NOT carry `wasLongDisconnect: true`; the IndexedDB SHALL NOT be cleared; the WS SHALL resume and append new envelopes to the existing in-memory ring; missed envelopes during the short disconnect window are simply lost from the in-memory ring (broker's at-most-once delivery semantics) — the IndexedDB hydrate on the next page navigation does not replay them

#### Scenario: Per-page navigation does not trigger a wipe

- **WHEN** the operator navigates between admin pages (e.g. `/messages` → `/settings`) within the same session
- **THEN** the base template's `app.js` SHALL hydrate the in-memory ring from the existing IndexedDB (no wipe); the WS SHALL reconnect; new envelopes SHALL be appended to the hydrated ring

### Requirement: Ring buffer and SignConfig are hydrated from IndexedDB on page navigation

On every admin page load, the base template's `app.js` SHALL call `MessageBufferStore.hydrate()` to load the most recent 100 messages (newest first, by `received_at`) into the `MessageManager`'s `InMemoryMessages` ring and SHALL load the `SignConfig` from the `config` object store. The hydration SHALL be async and SHALL complete before the `/preview` page requests the first frame from `PreviewCoordinator`. The hydration SHALL use the existing IndexedDB (no wipe) on per-page navigation; the wipe is reserved for app start, login, and long-disconnect reconnect (per the previous requirement).

#### Scenario: Hydration on reload restores the last ring

- **WHEN** a user reloads the preview page after several messages have been dispatched
- **THEN** `hydrate()` SHALL populate the in-memory ring with the most recent 100 messages (in `received_at` descending order); the `SignConfig` SHALL be restored from the `config` object store; the preview SHALL show the most recent message immediately, before the WS connection completes

#### Scenario: Hydration on per-page navigation restores the last ring

- **WHEN** a user navigates from `/messages` to `/settings` within the same session
- **THEN** `hydrate()` SHALL populate the in-memory ring from the existing IndexedDB (no wipe); the WS SHALL reconnect on the new page; subsequent envelopes SHALL be appended to the hydrated ring

#### Scenario: Empty IndexedDB on first visit

- **WHEN** a user opens an admin page for the first time in a new browser profile (no prior IndexedDB data, no wipe-re-seed cycle has run)
- **THEN** `hydrate()` SHALL return an empty ring buffer and a default `SignConfig`; the wipe + re-seed sequence SHALL then run as in the app-start scenario, populating the ring from `/api/messages` and the config from `/api/config`

#### Scenario: Hydration precedes the first frame

- **WHEN** PyScript finishes initializing on the `/preview` page
- **THEN** the browser SHALL call `hydrate()` and await its completion before the `requestAnimationFrame` loop starts; the first frame SHALL reflect the hydrated state, not an empty state

### Requirement: Persistence writes do not block the hot path

IndexedDB writes are asynchronous. `MessageManager.dispatch()` SHALL return to the caller immediately after the in-memory state is updated and the `on_message` callback has been invoked; the IndexedDB write SHALL be issued in the same call but SHALL NOT block the dispatch path. A write failure (quota exceeded, private-browsing mode, IndexedDB unavailable) SHALL be logged as a non-fatal warning and SHALL NOT raise into the dispatch path — the in-memory state remains correct, the next reload may simply miss the unwritten entries.

#### Scenario: A failed IndexedDB write is logged, not raised

- **WHEN** the IndexedDB write triggered by `dispatch()` rejects (e.g. quota, private mode)
- **THEN** the dispatch path SHALL complete normally; the in-memory ring buffer and `SignConfig` SHALL be updated; a warning SHALL be logged to the browser console; the user SHALL see a non-fatal "Persistence unavailable" indicator in the connection status block; no exception SHALL propagate to the MQTT-WS client or to the `on_message` callback

#### Scenario: A successful IndexedDB write is fire-and-forget

- **WHEN** the IndexedDB write triggered by `dispatch()` resolves
- **THEN** no result is returned to the dispatch path; the next dispatch may proceed without waiting for the prior write to resolve

### Requirement: Persistence is per-origin and per-browser

IndexedDB is partitioned by browser origin and browser profile. The persisted state SHALL be visible only to the same browser, on the same origin, signed in to the same browser profile. Two different browsers, two different browser profiles, or a private-browsing window SHALL each have an independent persistence store. There SHALL be no cross-device or cross-profile sync in v1.

#### Scenario: A different browser profile sees an empty store

- **WHEN** a user opens the preview in a different browser profile (or a different browser, or a private window)
- **THEN** `hydrate()` SHALL return an empty ring and a default `SignConfig`; the wipe + re-seed sequence SHALL then run as in the app-start scenario

#### Scenario: A user clearing browser data wipes the persistence

- **WHEN** a user clears site data for the preview's origin
- **THEN** the session-marker flag in `localStorage` SHALL also be cleared (or the next page load SHALL detect a stale marker and force a wipe); the wipe + re-seed sequence SHALL then run; the preview SHALL start fresh, identical to a first-time visit

### Requirement: Ring buffer is trimmed to the most recent 100 messages in storage

When a new message is added to the ring buffer, if the total in-storage message count exceeds 100, the oldest message(s) by `received_at` SHALL be removed from the `messages` object store. The trim SHALL happen in the same IndexedDB write transaction as the new message's `put`, so a partial failure cannot leave the store in a 101-message state.

#### Scenario: A 101st message trims the oldest in the same transaction

- **WHEN** the in-memory ring has 100 messages and a 101st message is added
- **THEN** the IndexedDB write transaction SHALL `put` the new message and SHALL `delete` the oldest message by `received_at`; after the transaction resolves, the `messages` object store SHALL contain exactly 100 entries
