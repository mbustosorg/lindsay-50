## Why

The browser preview currently behaves like a page fragment: navigating through the admin UI tears down and reconstructs its simulated-Pi runtime, which in turn requires cross-page browser persistence and obscures whether a message came from the REST seed or the live MQTT path. Making the preview the long-lived dashboard gives operators one stable control surface that more faithfully exercises the Pi lifecycle while consolidating duplicated testing, diagnostic, and message-management interfaces.

## What Changes

- Make `GET /` the primary, auto-starting preview dashboard; the dashboard tab owns one complete simulated-Pi runtime until it is stopped, reset, refreshed, or closed.
- Add Start and Stop controls for the whole simulator. Stop tears down rendering and MQTT reception; Start always constructs a fresh coordinator and message runtime, seeds it from REST, reconnects MQTT, and begins from its initial state rather than resuming.
- Keep the dashboard alive by replacing the in-place left navigation with explicit links that open Settings, Testing, and Messages in separate tabs/windows; preserve Logout behavior.
- Consolidate the existing dashboard list and Testing-page feed into one dashboard table covering the most recent 100 messages, with client-side pagination, suppression state, suppress/unsuppress actions, and an explicit REST-seed versus live-MQTT receipt indicator.
- Preserve `/messages` as a separate, server-authoritative, paginated archive of all messages, opened from the dashboard in a new tab and structured to accept future management capabilities such as permanent deletion (deletion itself is out of scope).
- Move test-message injection onto the dashboard.
- Expose Current Config, Active Filters, and S3 Bucket Browser as dashboard modals so operators can inspect them without navigating away.
- Retain the existing Testing page as a transitional legacy/advanced tool, while making the dashboard the canonical home for its duplicated controls and diagnostics; removing Testing is deferred until the dashboard is proven.
- Retire the cross-navigation `sessionStorage` message/config seed cache and the every-page simulated-Pi bootstrap. The running dashboard keeps simulator state in memory; refresh intentionally resets and upgrades it, while non-dashboard pages do not instantiate a second simulated-Pi runtime.
- Redirect the legacy standalone Preview UI route to the dashboard while preserving the preview's underlying APIs and keeping the all-message `/messages` page.

## Capabilities

### New Capabilities
- `standalone-preview-dashboard`: Covers the long-lived main dashboard, complete simulator start/stop/reset lifecycle, dashboard-preserving navigation, diagnostic modals, test injection, and removal of cross-page browser-runtime persistence.
- `dashboard-message-management`: Covers the integrated recent-100 dashboard table, pagination, receipt-path indicators, suppression actions, and the separate all-message archive.

### Modified Capabilities

None.

## Impact

- Flask routes and template composition in `heart-message-manager/main.py` and `heart-message-manager/templates/`, especially the dashboard, preview, messages, testing, settings, and shared navigation.
- The PyScript preview bootstrap and browser MQTT wrappers in `heart-message-manager/static/`, plus the shared Python `MessageManager`, its `sessionStorage` cache helpers, the in-memory browser `EventLog` subclass (replacing the prior `IndexedDBEventLog`), and `EffectsCoordinator` integration used by the browser runtime.
- The simulated-Pi bootstrap and message/config cache become dashboard-scoped or are removed; native JS remains limited to browser I/O shims, with simulator and message behavior continuing to use shared Python from `lib_shared/`. The browser selector event log becomes an in-memory bounded queue owned by the running generation; IndexedDB-based event log persistence for the browser is removed.
- Existing message/config/filter/S3 REST endpoints remain the authority and are reused by the dashboard; message suppression endpoints keep their current contract.
- Template, Flask-route, PyScript-unit, and browser end-to-end coverage must be updated for lifecycle teardown/restart, source attribution, pagination/actions, modal diagnostics, navigation, and refresh-reset behavior.
