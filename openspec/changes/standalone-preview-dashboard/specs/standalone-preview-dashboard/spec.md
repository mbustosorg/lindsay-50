## ADDED Requirements

### Requirement: The preview dashboard is the primary long-lived admin surface

Authenticated `GET /` SHALL render the browser sign simulator, simulator controls, recent-message management, test-message injection, and diagnostic links on one dashboard. The dashboard SHALL NOT render the existing left-side navigation, because secondary admin destinations do not replace the running dashboard in its browsing context.

The legacy standalone preview route SHALL redirect to the dashboard. Logout SHALL retain its existing behavior.

#### Scenario: Dashboard loads as the main application
- **WHEN** an authenticated operator visits `/`
- **THEN** the response contains the sign canvas, simulator lifecycle controls, recent-message table, test-message form, and links for Settings, Testing, and Messages

#### Scenario: Left-side navigation is absent
- **WHEN** the dashboard is rendered
- **THEN** it does not contain the existing left-side navigation component or an in-place page-navigation affordance

#### Scenario: Secondary tools open without replacing the dashboard
- **WHEN** the operator activates the Settings, Testing, or Messages link
- **THEN** the destination opens in a new browsing context and the original dashboard document and simulator runtime remain loaded

#### Scenario: Legacy preview URL enters the dashboard
- **WHEN** an authenticated operator visits the legacy `/preview` route
- **THEN** the server redirects to the dashboard preview section rather than creating a second preview runtime

### Requirement: The complete browser simulator has explicit fresh-start and stop lifecycles

The dashboard SHALL automatically start one complete simulated-Pi runtime after its browser dependencies are ready. The runtime SHALL include the shared Python `MessageManager`, in-memory message/config state, MQTT-over-WebSocket subscription, effects coordinator, scroller, render loop, and the in-memory browser selector event log.

Stop SHALL tear down the complete runtime as if the Pi were powered off: the MQTT subscription SHALL be disconnected, the `MessageManager` and its in-memory ring SHALL be discarded, the coordinator/scroller/canvas/render loop SHALL be released, the in-memory browser selector event log SHALL be discarded, and runtime callbacks/timers SHALL be released. After Stop, no inbound message or config envelope can mutate the stopped runtime. A subsequent Start SHALL create a new runtime generation with a new in-memory event log, construct a new `MessageManager`, REST-seed it, reconnect MQTT, and begin rendering from its initial state. It SHALL NOT resume the stopped generation.

#### Scenario: Initial dashboard load auto-starts a fresh simulator
- **WHEN** the dashboard finishes loading PyScript and its browser I/O shims
- **THEN** it creates one fresh simulator generation, seeds it from the configured REST APIs, connects it to the configured MQTT message topic, and starts rendering

#### Scenario: Stop tears down the whole simulator
- **WHEN** the operator activates Stop while the simulator is running
- **THEN** the MQTT-over-WebSocket subscription is disconnected, the shared Python `MessageManager` and its in-memory message/config state are released, the effects coordinator/scroller/canvas/render loop are cancelled, runtime callbacks and timers are released, the simulator status becomes `stopped`, and no subsequent message or config envelope mutates the stopped runtime

#### Scenario: Stopped runtime discards its selection history
- **WHEN** the operator activates Stop while the simulator is running
- **THEN** the browser selector event log for the stopped generation is discarded along with the rest of the runtime, so a later Start begins with no display-recency carry-over

#### Scenario: Start after Stop resets rather than resumes
- **WHEN** the operator activates Start after stopping the simulator
- **THEN** the dashboard creates a new runtime generation with a fresh `MessageManager` and in-memory ring, a new in-memory browser selector event log, new coordinator/effect/scroller instances, performs a new REST seed, reconnects MQTT, and does not reuse the stopped generation's cycle position, in-memory ring, or selection history

#### Scenario: Stale callbacks cannot cross runtime generations
- **WHEN** a delayed callback from an earlier simulator generation fires after a new generation has started
- **THEN** the callback is ignored using the runtime generation identity and cannot update the current canvas, messages, config, or status

#### Scenario: Runtime startup failure remains recoverable
- **WHEN** REST seeding, MQTT connection, effect construction, or another startup step fails
- **THEN** the dashboard enters an error state with an actionable message, releases any partially-created runtime resources, and permits the operator to activate Start for a fresh attempt

### Requirement: The dashboard simulator exercises REST seed and live MQTT as distinct paths

Each fresh simulator generation SHALL seed its shared Python `MessageManager` from the existing messages and config REST APIs and then receive subsequent message/config envelopes through the browser MQTT-over-WebSocket wrapper. Both paths SHALL feed the same in-memory shared-Python runtime used by the Pi; browser business logic SHALL NOT be reimplemented as a parallel JavaScript `MessageManager`, filter engine, selector, coordinator, or message model.

#### Scenario: REST seed establishes initial simulator state
- **WHEN** a fresh simulator generation starts
- **THEN** it fetches the canonical message history and current config through the same authenticated REST contracts used by the Pi and populates its in-memory Python state before displaying seeded content

#### Scenario: Live MQTT updates the running simulator
- **WHEN** the running dashboard receives a valid message or config envelope from its subscribed MQTT topic
- **THEN** the browser wrapper passes the raw envelope to the shared Python `MessageManager`, which updates the same in-memory state that was populated by the REST seed

#### Scenario: Browser I/O remains a shim around shared Python behavior
- **WHEN** the dashboard runtime is inspected
- **THEN** native JavaScript is limited to browser I/O and DOM/canvas interop, while message parsing, filtering, selection, coordination, and model behavior are imported from the shared Python implementation

### Requirement: Simulator state is in-memory and scoped to the dashboard document

The dashboard SHALL NOT restore simulator messages, config, or selection history from `sessionStorage`, IndexedDB, local storage, or another cross-navigation browser cache. The browser selector event log SHALL be a bounded in-memory queue owned by the current generation; each fresh simulator generation SHALL discard the prior queue and create a new one so Stop then Start and refresh have reset semantics. Refreshing or closing the dashboard SHALL destroy its in-memory simulator state; a refreshed dashboard SHALL load the current application assets and start a fresh REST-seeded generation.

Non-dashboard pages SHALL NOT bootstrap the simulated-Pi message/config MQTT subscription, `MessageManager`, preview coordinator, or cross-page persistence layer. A page-specific subscription used solely for physical-sign health MAY continue independently.

#### Scenario: Dashboard refresh intentionally resets the simulator
- **WHEN** the operator refreshes the dashboard
- **THEN** the prior runtime is discarded, the browser loads the currently deployed assets, and a fresh simulator generation starts from a new REST seed without hydrating prior browser state

#### Scenario: Fresh generation creates a new in-memory selector event log
- **WHEN** the dashboard creates a new simulator generation after initial load, refresh, or Stop then Start
- **THEN** it discards the prior in-memory browser selector event queue and constructs a new bounded queue before the new generation selects a message

#### Scenario: In-memory selector log evicts oldest entries at the cap
- **WHEN** the running simulator appends more than the bounded queue's capacity (default 100 entries)
- **THEN** the queue drops the oldest entries to remain at the cap; selection reads return only the entries still present in the queue

#### Scenario: Secondary page does not create another simulator
- **WHEN** Settings, Testing, or Messages opens in another tab
- **THEN** that page does not instantiate a simulated-Pi message/config MQTT client, browser `MessageManager`, preview coordinator, or browser selector event log

#### Scenario: Closing a secondary tab does not affect the dashboard
- **WHEN** the operator closes a Settings, Testing, or Messages tab
- **THEN** the original dashboard's simulator generation, MQTT connection, and render loop continue unchanged

### Requirement: Dashboard diagnostics open in non-navigating modals

The dashboard SHALL expose Current Config, Active Filters, and S3 Bucket Browser links that open accessible modal dialogs in the same document without replacing or resetting the simulator.

Current Config SHALL display the exact serialized config currently held by the running simulator generation. Active Filters SHALL be derived from that same config. The S3 Bucket Browser SHALL use the existing authenticated server API and SHALL NOT embed S3 credentials or signed-object credentials in the application source.

#### Scenario: Current Config reflects the running simulator
- **WHEN** the operator opens Current Config while the simulator is running
- **THEN** the modal displays the config object currently used by that simulator generation, including config envelopes applied since its REST seed

#### Scenario: Active Filters use the simulator config
- **WHEN** the operator opens Active Filters
- **THEN** the modal lists the active filter rules from the running simulator's current shared Python config rather than an independently cached browser copy

#### Scenario: S3 browser loads without navigation
- **WHEN** the operator opens the S3 Bucket Browser
- **THEN** the modal fetches and renders the existing authenticated S3 listing while the simulator continues rendering and receiving MQTT envelopes

#### Scenario: Closing a diagnostic modal preserves runtime state
- **WHEN** the operator closes any diagnostic modal
- **THEN** the simulator generation, effect position, scroller state, message ring, config, and MQTT connection are unchanged

### Requirement: Dashboard test injection distinguishes Flask acceptance from simulator receipt

The dashboard SHALL provide the existing test-message injection capability. Submission SHALL use the existing Flask test-message endpoint. The UI SHALL report Flask acceptance separately from observation of the corresponding message through the running simulator's MQTT path; a successful HTTP response SHALL NOT be presented as proof that the simulator received the MQTT envelope.

#### Scenario: Running simulator observes an injected message over MQTT
- **WHEN** Flask accepts a dashboard test-message submission and the broker delivers the resulting envelope to the running simulator
- **THEN** the UI first reports Flask acceptance and subsequently marks that message as received through MQTT when the simulator dispatches the envelope

#### Scenario: Stopped simulator does not claim MQTT receipt
- **WHEN** Flask accepts a test message while the simulator is stopped
- **THEN** the UI reports Flask acceptance but does not mark the message as MQTT-received by the stopped simulator

#### Scenario: Failed injection is not added optimistically
- **WHEN** the test-message endpoint returns an error
- **THEN** the dashboard displays the error and does not add a synthetic message or MQTT-receipt marker to the simulator table

### Requirement: Existing secondary pages remain available during migration

Settings SHALL retain its existing functionality. Testing SHALL remain available as a transitional legacy/advanced tool until the dashboard replacement has been proven. Messages SHALL remain a separate all-message archive. The dashboard SHALL link to each destination in a new tab without requiring those pages to participate in the dashboard simulator lifecycle.

#### Scenario: Testing remains reachable
- **WHEN** the operator opens the dashboard's Testing link
- **THEN** the existing Testing page opens in a new tab and its current supported tools remain available

#### Scenario: Settings remains reachable
- **WHEN** the operator opens the dashboard's Settings link
- **THEN** the existing Settings page opens in a new tab with its existing configuration-editing behavior

#### Scenario: Transitional Testing page is not made a runtime dependency
- **WHEN** the dashboard is running and the Testing page is closed or unavailable
- **THEN** preview rendering, message reception, diagnostics, and test-message injection on the dashboard continue to work
