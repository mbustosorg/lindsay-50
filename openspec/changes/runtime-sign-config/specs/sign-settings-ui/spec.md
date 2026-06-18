## ADDED Requirements

### Requirement: Settings page surfaces the effects rotation as toggles in an Effects → Effects List sub-section

The admin UI's `/settings` page SHALL include one big "Effects" section (the "what does the sign do?" panel). Inside it, an "Effects List" sub-section SHALL render one checkbox per entry in `cfg.effects_settings.effects`, in the order returned by `GET /api/config`. The list is a full 7-entry list of `{"name": str, "enabled": bool}` objects — the page SHALL render all 7 entries (the operator sees the disabled effects too, in their canonical rotation position). Each checkbox SHALL be checked if `enabled == true`, unchecked otherwise. The form submission SHALL submit the full list with each entry's `enabled` flag set to the checkbox state — the position of each effect in the rotation SHALL be preserved (the operator never reorders effects in v1).

The sub-section is named "Effects List" (not "Rotation") because "Rotation" implies cycling, but the enabled set is just a list — the coordinator's cycle order is also driven by this list, but that is an implementation detail, not the operator's mental model. The Effects section is the umbrella for the effects list AND the timing / behavior sliders (next requirement); a separate "Pacing" top-level section is NOT created, because future fields added to `effects_settings` (transition modes, boot-splash toggles, etc.) won't all be pacing-related. The Text section is a separate top-level section (not a sub-section of Effects) because the text renderer is a different subsystem.

The full canonical set of effect names is defined in the device's `_DEFAULT_EFFECTS_LIST` constant; the `cfg.effects_settings.effects` payload is the source of truth for both the order and the enabled/disabled state. No `GET /api/effects` call is made by the page (that endpoint was dropped in this change; the data lives in `GET /api/config`).

#### Scenario: Default state shows 5 checked + 2 unchecked
- **WHEN** the operator visits `/settings` and the current `cfg.effects_settings.effects` is the default 7-entry list with 5 enabled (Hyperspace, Honeycomb, Flame, Fireworks, NightSky) and 2 disabled (VideoDisplay, PngDisplay)
- **THEN** the Effects List sub-section SHALL show all 7 checkboxes in canonical rotation order; the 5 enabled ones SHALL be checked, and the 2 disabled ones (VideoDisplay, PngDisplay) SHALL be unchecked

#### Scenario: An operator disables an effect
- **WHEN** the operator unchecks "Honeycomb" and saves
- **THEN** the new `cfg.effects_settings.effects` SHALL preserve the 7-entry list order, with Honeycomb's `enabled` flag set to `false` (the other 6 entries SHALL retain their previous `enabled` values); the response from `PUT /api/config` SHALL be HTTP 200

#### Scenario: An operator enables a previously disabled effect
- **WHEN** the operator re-checks a previously unchecked "VideoDisplay" and saves
- **THEN** the new `cfg.effects_settings.effects` SHALL preserve the 7-entry list order, with VideoDisplay's `enabled` flag set to `true` (VideoDisplay's position in the rotation list is unchanged — the page does not move effects around based on enabled state)

#### Scenario: Unknown effect names in the config are surfaced as disabled
- **WHEN** `cfg.effects_settings.effects` contains a name not in the device's canonical effect set (e.g. a stale config from a future effect that was removed)
- **THEN** the unknown name SHALL be rendered as a disabled, struck-through label in the Effects List (the operator can see something is off but cannot toggle it on); the canonical 7 known effects SHALL render as normal checkboxes; the device's WARN-and-skip path remains the safety net

### Requirement: Settings page surfaces the effect behavior as labeled sliders in an Effects → Settings sub-section, including recent_count

The `/settings` page SHALL include a "Settings" sub-section inside the Effects section. (The sub-section is intentionally named "Settings" and not "Behavior" or "Pacing" — it is the most generic label, since future fields added to the block could be any kind of control, not all pacing-related. The Scrolling section is a separate top-level section and will get its own sub-section naming when it needs more than one.) The Settings sub-section SHALL have five labeled controls, each rendered as a range slider paired with a number input that the slider sets:

- **Fade speed** (label; wire name `effects_settings.fade_seconds`): range 0.1–10.0, step 0.1, default 2.0.
- **Hold time** (label; wire name `effects_settings.hold_seconds`): range 1–120, step 1, default 15.
- **Intro time** (label; wire name `effects_settings.intro_seconds`): range 0–30, step 0.5, default 5.
- **Idle time** (label; wire name `effects_settings.idle_seconds`): range 30–3600, step 30, default 300.
- **Recent messages** (label; wire name `effects_settings.recent_count`): range 1–20, step 1, default 5. Surfaced per operator feedback — it controls the size of the idle-rotation pool, which is a visible experience knob.

Each label SHALL be the human-readable name above; the wire name is internal. The number input SHALL be linked to the slider via JS so editing one updates the other. Saving the form SHALL submit the number input's value as the field value.

#### Scenario: Settings fields render with the current config's values
- **WHEN** the operator visits `/settings` and the current `cfg.effects_settings.fade_seconds` is 3.5
- **THEN** the "Fade speed" slider SHALL be at 3.5 and the number input SHALL show 3.5

#### Scenario: An operator changes the fade speed
- **WHEN** the operator moves the "Fade speed" slider to 5.0 and saves
- **THEN** the new `cfg.effects_settings.fade_seconds` SHALL be 5.0, and `PUT /api/config` SHALL return HTTP 200

#### Scenario: Settings fields with out-of-range values are rejected
- **WHEN** the operator submits a form with `effects_settings.fade_seconds = -1` (a value outside the 0.1–10.0 range)
- **THEN** `PUT /api/config` SHALL return HTTP 400 with a per-field error message, and the in-memory config SHALL NOT be updated

#### Scenario: Settings fields with non-numeric values are rejected
- **WHEN** the operator submits a form with `effects_settings.hold_seconds = "abc"`
- **THEN** `PUT /api/config` SHALL return HTTP 400

#### Scenario: An operator changes recent_count
- **WHEN** the operator moves the "Recent messages" slider to 10 and saves
- **THEN** the new `cfg.effects_settings.recent_count` SHALL be 10, and `PUT /api/config` SHALL return HTTP 200

#### Scenario: An operator submits recent_count = 0
- **WHEN** the operator submits a form with `effects_settings.recent_count = 0`
- **THEN** `PUT /api/config` SHALL return HTTP 400 (the validation requires `recent_count >= 1`)

#### Scenario: An operator submits non-integer recent_count
- **WHEN** the operator submits a form with `effects_settings.recent_count = 3.5`
- **THEN** `PUT /api/config` SHALL return HTTP 400 with a per-field error message (the validation requires an integer)

### Requirement: Settings page surfaces scroll speed, text color, and text effect in a Text section

The `/settings` page SHALL include a separate "Text" section (not a sub-section of Effects — the text renderer is a different subsystem) with:

- **Scroll speed** (wire name `text_settings.frame_delay`, inverse in the UI): a range slider where 0 = slow (highest `frame_delay`, e.g. 0.1 s/pixel) and 100 = fast (lowest `frame_delay`, e.g. 0.01 s/pixel). The wire stores `frame_delay` in seconds per pixel; the UI does the inverse mapping.
- **Text color** (wire name `text_settings.color`): a `<input type="color">` plus a hex text input, mirroring the existing single color field's pattern. Defaults to `#ff0000`.
- **Text effect** (wire name `text_settings.text_effect`): a `<select>` with one option, "scroll", in v1. The select SHALL be rendered as disabled with a tooltip "More text effects coming soon." The selected value SHALL still be submitted in the form payload (as `text_settings.text_effect=scroll`).

The section is named "Text" (not "Scrolling") because the scroller is one of several text effects that will share the `text_settings` block (swirl, bounce, etc., are planned). The previous "Rendering Defaults" section is removed — the rendering knobs it carried (speed, font, font_size, color, letter_spacing, line_spacing, bg_color, align) are being superseded by `text_settings` (for color and per-effect-text fields) and `effects_settings` (for the rotation pacing).

#### Scenario: Scroll speed slider updates frame_delay in seconds per pixel
- **WHEN** the operator moves the "Scroll speed" slider to 50 and saves
- **THEN** the new `cfg.text_settings.frame_delay` SHALL be the inverse-mapped value (e.g. `0.055` for the 0–100 → 0.1–0.01 mapping at 50%), and `PUT /api/config` SHALL return HTTP 200

#### Scenario: Text color input round-trips
- **WHEN** the operator sets the "Text color" hex input to `#00ff00` and saves
- **THEN** the new `cfg.text_settings.color` SHALL be `0x00FF00`

#### Scenario: Text effect dropdown is disabled in v1
- **WHEN** the operator visits `/settings`
- **THEN** the "Text effect" select SHALL be present, SHALL have "scroll" as its only option, and SHALL be marked disabled with a tooltip

#### Scenario: The old Rendering Defaults section is gone
- **WHEN** the operator visits `/settings`
- **THEN** the page SHALL NOT contain a "Rendering Defaults" section (the previous "Color" / "Font" / "Font size" / "Background" / "Letter spacing" / "Line spacing" / "Align" / "Speed" fields are replaced by the new Effects + Text sections). The "Speed" field's value is mapped to the new `effects_settings.fade_seconds` (the v2 mapping is a one-time default; existing v1 `rendering.speed` values are NOT carried over by the UI).

### Requirement: Form submission validates the new fields server-side

The Flask `PUT /api/config` handler SHALL validate the new fields before constructing a `SignConfig`:

- `effects_settings.effects` SHALL be a list of objects, each of which has a string `name` (a known effect class name from the device's canonical effect set — the same set used by `_DEFAULT_EFFECTS_LIST` on the device) and a boolean `enabled` flag. Entries whose `name` is not in the canonical set SHALL be rejected with HTTP 400 and a per-field error message. Missing `name` or `enabled` keys SHALL also be rejected. The server SHALL NOT silently drop unknown names; the client is told exactly which entry is bad.
- `effects_settings.fade_seconds`, `effects_settings.hold_seconds`, `effects_settings.intro_seconds`, `effects_settings.idle_seconds` SHALL each be a non-negative number. Out-of-range values SHALL be rejected with HTTP 400.
- `effects_settings.recent_count` SHALL be a positive integer. Out-of-range values SHALL be rejected with HTTP 400.
- `text_settings.frame_delay` SHALL be a non-negative number.
- `text_settings.offset_seconds` SHALL be a non-negative number.
- `text_settings.color` SHALL be an integer in the range 0–0xFFFFFF.
- `text_settings.text_effect` SHALL be one of the allowed enum values (currently `("scroll",)`).

The handler SHALL also run `migrate(...)` on the incoming payload (so v1 inputs are transparently upgraded to v2) before validation, and the saved SQLite row SHALL be at `version: 2` regardless of the input version. A v1 payload that includes `rendering` is accepted; the migration drops it and the saved config has no `rendering` key.

#### Scenario: An unknown effect name is rejected
- **WHEN** a client PUTs `{"effects_settings": {"effects": [{"name": "Flame", "enabled": true}, {"name": "DoesNotExist", "enabled": true}]}, "text_settings": {}}` to `/api/config`
- **THEN** the response SHALL be HTTP 400 with `{"error": "effects_settings.effects: unknown effect 'DoesNotExist'"}` (or similar), and the in-memory config SHALL NOT be updated

#### Scenario: A malformed effect entry is rejected
- **WHEN** a client PUTs `{"effects_settings": {"effects": [{"name": "Flame"}, {"enabled": true}]}, "text_settings": {}}` to `/api/config` (first entry has no `enabled`, second has no `name`)
- **THEN** the response SHALL be HTTP 400 with a per-field error message identifying both entries as malformed; the in-memory config SHALL NOT be updated

#### Scenario: Out-of-range behavior values are rejected
- **WHEN** a client PUTs `{"effects_settings": {"fade_seconds": -1}, "text_settings": {}}` to `/api/config`
- **THEN** the response SHALL be HTTP 400 with a per-field error message

#### Scenario: Out-of-range recent_count is rejected
- **WHEN** a client PUTs `{"effects_settings": {"recent_count": 0}, "text_settings": {}}` to `/api/config`
- **THEN** the response SHALL be HTTP 400

#### Scenario: An invalid text_effect is rejected
- **WHEN** a client PUTs `{"text_settings": {"text_effect": "swirl"}}` to `/api/config`
- **THEN** the response SHALL be HTTP 400 (since "swirl" is not in the v1 enum)

#### Scenario: A v1 payload is normalized to v2 before save
- **WHEN** a client PUTs a v1 payload (no `version` key, has `tz_offset_mins` and `rendering`) to `/api/config`
- **THEN** the response SHALL be HTTP 200, the saved SQLite row SHALL have `version: 2` and SHALL NOT contain `tz_offset_mins` or `rendering` keys, the new `effects_settings` and `text_settings` blocks SHALL be present with their defaults, and the published `type="config"` envelope SHALL be at v2 (verified by a follow-up `GET /api/config`)

#### Scenario: Valid input is accepted
- **WHEN** a client PUTs a well-formed payload with all new fields
- **THEN** the response SHALL be HTTP 200 with `{"status": "ok"}`, the new values SHALL be persisted to SQLite, an S3 snapshot SHALL be written, and a `type="config"` envelope SHALL be published to MQTT
