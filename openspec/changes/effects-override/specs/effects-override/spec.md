# Spec: effects-override

## ADDED Requirements

### Requirement: The system SHALL declare the canonical `effects_settings.json` mirroring the `EffectsSettings` dataclass shape

The repo SHALL ship `lib_shared/config/effects_settings.json` (in git, under the new `lib_shared/config/` folder that will hold all in-git config JSON files) as the single declarative source of truth for the `EffectsSettings` portion of `SignConfig`. The file MUST declare the exact fields the `EffectsSettings` dataclass accepts: `effects` (a list of `{"name": str, "enabled": bool, "module": str, "class_name": str}` dicts), `fade_seconds` (float), `hold_seconds` (float), `intro_seconds` (float), `idle_seconds` (float), `recent_count` (int), plus a top-level `schema_version` (int) for the loader's migration policy. The file SHALL declare all 7 effects; the asset-dependent effects (`PngDisplay`, `VideoDisplay`) SHALL carry `enabled: false` so the Flask admin UI surfaces the toggle surface while the rotation is safe on a fresh checkout.

#### Scenario: Fresh checkout, no override file present

- WHEN the system starts on a fresh repo checkout and `config_overrides/effects_settings.json` does not exist
- THEN `load_effects_settings()` SHALL return the contents of `lib_shared/config/effects_settings.json` parsed as JSON
- AND the parsed dict SHALL contain `effects` (list of 7 entries), `fade_seconds`, `hold_seconds`, `intro_seconds`, `idle_seconds`, and `recent_count`
- AND `effects` SHALL be a list of dicts each having `name`, `enabled`, `module`, `class_name` populated
- AND the top-level keys SHALL be an exact subset of `EffectsSettings.__init__` parameters plus `schema_version`

#### Scenario: Effect entry schema validation

- WHEN `load_effects_settings()` returns the canonical config
- THEN every `module` value SHALL be importable via `importlib.import_module(...)` without error
- AND every `class_name` value SHALL be retrievable via `getattr(module, class_name)` and SHALL be a subclass of `rgb_display.Effect` or equivalent base class

#### Scenario: `EffectsSettings()` constructor reads canonical defaults

- WHEN `EffectsSettings()` is constructed with no arguments and no override is active
- THEN its `effects` attribute SHALL equal the canonical JSON's `effects` list (with `module` and `class_name` stripped, since the dataclass only needs `name` and `enabled`)
- AND its `fade_seconds`, `hold_seconds`, `intro_seconds`, `idle_seconds`, `recent_count` attributes SHALL equal the canonical JSON's corresponding fields

### Requirement: The system SHALL support an override file at `config_overrides/effects_settings.json`

The repo SHALL treat `config_overrides/effects_settings.json` (in a gitignored folder) as the operator's override of the canonical `effects_settings`, with REPLACE semantics — not merge.

#### Scenario: Override file present and parses cleanly

- WHEN `config_overrides/effects_settings.json` exists at the repo root and is a valid JSON file matching the canonical schema
- THEN `load_effects_settings()` SHALL return the contents of that file
- AND `is_effects_settings_override_active()` SHALL return `True`

#### Scenario: Override file missing

- WHEN `config_overrides/effects_settings.json` does not exist
- THEN `load_effects_settings()` SHALL return the contents of `lib_shared/config/effects_settings.json`
- AND `is_effects_settings_override_active()` SHALL return `False`

#### Scenario: Operator sets `EFFECTS_SETTINGS_OVERRIDE` env var

- WHEN the operator sets `EFFECTS_SETTINGS_OVERRIDE` to a path and that file exists
- THEN `load_effects_settings()` SHALL return the contents of that path
- AND `is_effects_settings_override_active()` SHALL return `True`
- AND this behavior SHALL take precedence over the `config_overrides/effects_settings.json` lookup

#### Scenario: `EFFECTS_SETTINGS_OVERRIDE` env var points to a missing file

- WHEN the env var is set to a path but that path does not exist
- THEN the loader SHALL log a warning and fall back to the next precedence level (repo-root override file, then canonical)

### Requirement: Override semantics SHALL be REPLACE, not MERGE

The loader SHALL NOT combine fields from the canonical and override files. If the operator's override lists 3 effects, the system SHALL treat those 3 as the entire registry — neither supplementing nor filtering the canonical 7. If the operator's override sets `recent_count: 3`, that value replaces the canonical entirely.

#### Scenario: Override lists fewer effects than canonical

- WHEN the operator's override file contains 3 effects and the canonical contains 7
- THEN the loader SHALL return only those 3 effects
- AND `make_effect_class()` SHALL return `None` for any name from the canonical not present in the override

#### Scenario: Override lists an effect name not in canonical

- WHEN the operator's override contains an effect name that does not appear in the canonical
- THEN the loader SHALL still resolve it via the same dynamic-import mechanism as canonical names
- AND no validation against the canonical list SHALL occur

### Requirement: The factory SHALL resolve effect classes via dynamic import (from the loader)

`lib_shared.effects_loader.make_effect_class(name)` SHALL resolve a class name string to the corresponding class through the loader-driven config, using `importlib.import_module` plus `getattr`. The factory lives in the loader module — `lib_shared/effects_factory.py` does NOT exist as a separate file after this change.

#### Scenario: Canonical name, module present in sys.modules

- WHEN `make_effect_class("Fireworks")` is called and the canonical config lists `Fireworks` → `lib_shared.patterns.fireworks` → `Fireworks`
- THEN it SHALL return `lib_shared.patterns.fireworks.Fireworks`
- AND the import SHALL be lazy (numpy/cv2/PIL imports triggered only when `make_effect_class` is invoked, not at module load)

#### Scenario: Unknown name

- WHEN `make_effect_class("Unicorn")` is called and no entry in the loaded config matches that name
- THEN it SHALL return `None`
- AND it SHALL NOT raise an exception

#### Scenario: Module imports but class name is wrong

- WHEN the config entry's `class_name` does not exist in the resolved module
- THEN `make_effect_class()` SHALL raise `AttributeError` with a message identifying the missing attribute

### Requirement: The Pi SHALL ignore the wire's `effects_settings` block when the override is active

When `is_effects_settings_override_active()` is `True`, the Pi's `MessageManager._handle_config` SHALL pop `payload["effects_settings"]` from incoming wire config payloads before applying them. Top-level `text_settings`, `filters`, `senders`, `sign`, and `timezone` SHALL still be applied from the wire.

#### Scenario: Override active, wire sends full `effects_settings`

- WHEN the override file exists on the Pi
- AND Flask publishes an envelope containing `effects_settings = {"effects": [...], "fade_seconds": 5.0, ...}`
- THEN the manager SHALL pop the entire `effects_settings` key before calling `SignConfig.update_from_dict`
- AND the local `EffectsSettings` SHALL remain whatever the loader-derived config produces
- AND `text_settings`, `filters`, `senders`, `sign`, and `timezone` from the wire SHALL still be applied

#### Scenario: Override not active, wire sends full `effects_settings`

- WHEN the override file does not exist on the Pi
- THEN the manager SHALL apply `effects_settings` from the wire unchanged
- AND this behavior SHALL be identical to the pre-change behavior

#### Scenario: Override active, wire sends only `text_settings`

- WHEN the override file exists
- AND the wire sends only top-level `text_settings = {...}` (no `effects_settings` key)
- THEN the manager SHALL apply `text_settings` from the wire
- AND no field SHALL be stripped

### Requirement: The Flask admin UI SHALL derive its effect metadata from the loaded config

The Flask app SHALL derive the `_KNOWN_EFFECT_NAMES` set from `load_effects_settings()["effects"]` at startup (replacing the hardcoded frozenset). The `/settings` and `/playful/settings` routes SHALL pass the loaded config to their templates so the templates can render name, enabled state, module, and class_name without hardcoding.

#### Scenario: Server start, no override

- WHEN the Flask app starts and `config_overrides/effects_settings.json` does not exist
- THEN `_KNOWN_EFFECT_NAMES` SHALL be the set of `name` fields from `lib_shared/config/effects_settings.json`
- AND the admin `/settings` route SHALL receive `effects_settings = load_effects_settings()` and pass it to its template

#### Scenario: Server start, override present

- WHEN the Flask app starts and `config_overrides/effects_settings.json` exists
- THEN `_KNOWN_EFFECT_NAMES` SHALL be the set of `name` fields from that override file
- AND the admin UI SHALL render the override's metadata, not the canonical's

#### Scenario: Operator adds an effect name to the override file

- WHEN the operator adds `"MyNewEffect"` to `config_overrides/effects_settings.json` (with the matching `module` and `class_name`) and restarts the Flask app
- THEN `_KNOWN_EFFECT_NAMES` SHALL include `MyNewEffect`
- AND POSTing to `/api/config` with `effects_settings.effects` containing `MyNewEffect` SHALL be accepted (HTTP 2xx), not rejected (HTTP 400)
- AND the admin UI SHALL render the row for `MyNewEffect`

### Requirement: Settings templates SHALL render effect rows dynamically

`heart-message-manager/templates/settings.html` and `templates/playful/settings-playful.html` SHALL iterate `effects_settings.effects` for per-effect rows (name + enabled toggle + module/class_name caption), and SHALL render the timing fields (`fade_seconds`, `hold_seconds`, `intro_seconds`, `idle_seconds`, `recent_count`) from the same `effects_settings` dict. The templates SHALL NOT contain a hardcoded list of effect names or timing defaults.

#### Scenario: Template renders canonical 7 effects

- WHEN the template is rendered with `effects_settings.effects` containing the canonical 7 effects
- THEN the rendered HTML SHALL contain 7 effect rows
- AND each row SHALL include the effect's name, enabled toggle, and a caption showing module/class_name
- AND the timing field inputs SHALL be pre-populated with the canonical `fade_seconds`, `hold_seconds`, `intro_seconds`, `idle_seconds`, `recent_count`

#### Scenario: Template renders a subset via override

- WHEN the template is rendered with `effects_settings.effects` containing only 3 entries from an override
- THEN the rendered HTML SHALL contain only those 3 rows
- AND no canonical name NOT in the override SHALL appear

### Requirement: `_DEFAULT_EFFECTS_LIST_FULL` SHALL NOT exist; consumers use loader functions

`lib_shared/models.py` SHALL NOT define `_DEFAULT_EFFECTS_LIST_FULL` (or any alias, property, or module-`__getattr__` shim that re-exports it). All code that previously read this attribute MUST call `lib_shared.effects_loader.load_effects_settings()` directly. Tests that previously monkey-patched the alias MUST inject loader fixtures via `effects_loader.reset_effects_settings()` after setting a fake config dict.

#### Scenario: Consumer reads the loader directly

- WHEN any caller needs the canonical effects list
- THEN it SHALL call `lib_shared.effects_loader.load_effects_settings()["effects"]` (or a higher-level helper in the loader)
- AND `lib_shared.models._DEFAULT_EFFECTS_LIST_FULL` SHALL NOT be referenced anywhere in the codebase (verified by grep returning no matches)

#### Scenario: Test injects a fake config via loader fixture

- WHEN a test needs to override the loaded config for isolation
- THEN it SHALL call `lib_shared.effects_loader.reset_effects_settings()` after assigning a fake config to the loader's internal cache
- AND the test SHALL NOT reference `_DEFAULT_EFFECTS_LIST_FULL` (which no longer exists)

#### Scenario: Anti — module attribute does not exist

- **Anti:** `hasattr(lib_shared.models, "_DEFAULT_EFFECTS_LIST_FULL")` returns `True`. The alias is removed outright; even reading the attribute MUST raise `AttributeError`.

### Requirement: The `local_effects` kwarg on EffectsCoordinator SHALL be removed

`lib_shared/effects_coordinator.py::EffectsCoordinator.__init__` SHALL NOT accept a `local_effects` parameter. The `bind()`, `_tick_inner`, and any other methods that previously accepted or re-appended `local_effects` SHALL also be updated to drop the parameter. The `heart-matrix-controller/main.py` call site SHALL be updated to match.

#### Scenario: EffectsCoordinator constructed without local_effects

- WHEN `EffectsCoordinator(message_manager=..., display=..., scroller=..., effects=..., heart=...)` is called without the `local_effects` kwarg
- THEN construction SHALL succeed without error
- AND the coordinator's `self.effects` SHALL be exactly what `build_effects()` returned

#### Scenario: No re-append branch for local_effects in rotation rebuild

- WHEN the coordinator's `_tick_inner` performs the structural-diff rebuild
- THEN the rebuild SHALL ONLY append effects from `build_effects(snapshot)`
- AND no `_local_effects` attribute SHALL exist on the coordinator instance

### Requirement: `.gitignore` SHALL include `config_overrides/` as a single entry

The repo's `.gitignore` SHALL contain `config_overrides/` (one line, trailing slash for directory matching). No other entries specific to override files SHALL be required for this change.

#### Scenario: Operator creates an override file

- WHEN the operator creates `config_overrides/effects_settings.json` and runs `git status`
- THEN the new file SHALL appear as ignored (`ignored` in porcelain v1, or absent from `git status` output)
- AND no accidental commit SHALL surface in `git status`

#### Scenario: Future override files for other configs

- WHEN the operator creates `config_overrides/config.json` or `config_overrides/seed_messages.json` in a future change
- THEN those files SHALL also be ignored under the same single entry
- AND no change to `.gitignore` SHALL be required for those future overrides to work