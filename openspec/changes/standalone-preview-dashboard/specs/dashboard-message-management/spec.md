## ADDED Requirements

### Requirement: Dashboard table manages the simulator's most recent 100 messages

The dashboard SHALL display the running simulator generation's most recent 100 `MessageView` records, including both suppressed and non-suppressed messages. The table SHALL read the existing shared Python `MessageManager`/`InMemoryMessages` view rather than maintaining a second JavaScript message collection.

The dashboard SHALL paginate the 100-record browser view on the client with 20 rows per page. New runtime changes SHALL refresh the table without navigating or reloading the dashboard.

#### Scenario: REST seed populates the dashboard table
- **WHEN** a fresh simulator generation seeds 100 or fewer canonical messages from REST
- **THEN** the dashboard table renders those messages newest first, includes suppressed records, and shows no more than 20 rows on the first page

#### Scenario: Table is capped by the simulated Pi ring
- **WHEN** the running simulator has received more than 100 distinct messages
- **THEN** the dashboard table exposes only the most recent 100 records retained by its shared Python in-memory ring

#### Scenario: Client-side pagination covers the complete ring
- **WHEN** the simulator ring contains 100 messages
- **THEN** the dashboard exposes five 20-row pages and changing pages does not issue another message-history request or reset the simulator

#### Scenario: Live message refreshes the table in place
- **WHEN** the running simulator dispatches a new MQTT message envelope
- **THEN** the table updates from the simulator's shared Python state without a full-page reload while preserving a valid current page selection

#### Scenario: Suppressed messages remain visible
- **WHEN** the simulator ring contains a message excluded by an active filter
- **THEN** the message remains in the management table with a suppressed indicator and its matching rule information instead of being omitted

### Requirement: Dashboard rows distinguish REST-seeded and live-MQTT receipt

Each dashboard row SHALL display the existing `MessageView.source` value as an unambiguous receipt-path label: `rest` SHALL be presented as seeded through the canonical Flask REST API, and `mqtt` SHALL be presented as received live by the browser's simulated Pi subscription. The label describes how the current simulator generation obtained the record; it SHALL NOT claim that a REST-seeded record was observed over MQTT.

Source attribution SHALL use the existing shared Python `MessageView` contract and SHALL NOT introduce a browser-only message model or change the MQTT `Message` wire shape.

#### Scenario: REST-seeded message has a REST badge
- **WHEN** a message enters the simulator ring during `MessageManager.seed()`
- **THEN** its dashboard row displays a `REST seed` label or equivalent server-seed badge

#### Scenario: Live envelope has an MQTT badge
- **WHEN** a message enters the simulator ring through `MessageManager.dispatch()` from the browser MQTT client
- **THEN** its dashboard row displays an `MQTT live` label or equivalent simulated-Pi receipt badge

#### Scenario: Restart re-establishes session-local receipt semantics
- **WHEN** a message that was previously observed over MQTT is loaded through REST after the operator stops and freshly starts the simulator
- **THEN** the new generation labels that record as REST-seeded because REST is how that generation obtained it

### Requirement: Dashboard rows support suppress and unsuppress without navigation

Each dashboard message row SHALL show its current suppression state and SHALL expose the action valid for that state. Suppress and unsuppress SHALL call the existing authenticated message-suppression endpoints, prevent duplicate submissions while a request is pending, and update the dashboard from authoritative returned or re-fetched state without reloading the document.

#### Scenario: Operator suppresses a visible message
- **WHEN** the operator activates Suppress for a non-suppressed dashboard row and the server accepts the request
- **THEN** the row becomes suppressed, displays the applicable rule state, and the simulator/dashboard remain on the same runtime generation and page

#### Scenario: Operator unsuppresses a message
- **WHEN** the operator activates Unsuppress for a suppressed dashboard row and the server accepts the request
- **THEN** the suppression rule is removed, the row becomes non-suppressed, and the simulator/dashboard remain on the same runtime generation and page

#### Scenario: Suppression request fails
- **WHEN** a suppress or unsuppress request returns an error
- **THEN** the row retains its prior state, its action becomes available again, and the dashboard displays an actionable error without resetting the simulator

### Requirement: Messages page is the paginated archive of all canonical messages

Authenticated `GET /messages` SHALL remain a separate, server-authoritative archive backed by SQLite rather than the browser simulator's 100-message ring. It SHALL include all canonical messages, ordered newest first, in server-side pages of 50. The dashboard's Messages link SHALL open this route in a new browsing context.

The archive SHALL retain current message metadata, media presentation, suppression indicators, and suppress/unsuppress actions. It SHALL NOT instantiate the browser simulated-Pi runtime.

#### Scenario: Archive is not capped at 100 messages
- **WHEN** SQLite contains 175 canonical messages and the operator visits `/messages`
- **THEN** the archive reports all 175 records across four pages rather than truncating the result to the simulator's most recent 100

#### Scenario: Archive pagination is stable
- **WHEN** the operator visits `/messages?page=2`
- **THEN** the page renders canonical messages 51 through 100 in newest-first order with navigation to adjacent valid pages

#### Scenario: Dashboard opens archive separately
- **WHEN** the operator activates the dashboard's Messages link
- **THEN** `/messages` opens in a new browsing context and the dashboard's simulator continues running in its original tab

#### Scenario: Archive retains current management behavior
- **WHEN** an archived message has media or is suppressed
- **THEN** its row preserves the existing media presentation and suppression action available on the current Messages page

#### Scenario: Invalid archive page is handled safely
- **WHEN** the requested page number is absent, malformed, below one, or beyond the final page
- **THEN** the server renders a valid bounded archive page or an explicit not-found response without raising an unhandled error

### Requirement: Archive rows expose stable hooks for future message actions

Every Messages archive row SHALL expose the message identifier and received timestamp as stable row data attributes. These hooks SHALL allow later actions, including permanent deletion, to target a canonical row without redesigning the table. This change SHALL NOT add a delete button, delete endpoint, or deletion behavior.

#### Scenario: Archive row carries future-action identity
- **WHEN** `/messages` renders a canonical message row
- **THEN** the row includes `data-msg-id` and `data-received-at` values matching that message

#### Scenario: Permanent deletion is not exposed yet
- **WHEN** the operator views the Messages archive after this change
- **THEN** no permanent-delete control or message-delete API is introduced by this capability
