## Context

The current effect registration is a hardcoded Python registry in `lib_shared/effects_factory.py::make_effect_class()` — a chain of `if name == "X": from lib_shared.patterns.x import X; return X` branches. The Flask side mirrors this with a hardcoded `_KNOWN_EFFECT_NAMES` frozenset in `heart-message-manager/main.py`. The two registries must stay in lockstep — adding a new pattern today requires coordinated edits to both files plus `lib_shared/models.py::_DEFAULT_EFFECTS_LIST_FULL`.

A recently-merged recipe ([docs/adding-patterns.md](../../../docs/adding-patterns.md)) added a `local_effects` kwarg on `EffectsCoordinator` as a transitional mechanism for Pi-local pattern development. The user has rejected the transitional path entirely — this change **reverts** the recipe's `local_effects` additions as part of the broader JSON-driven refactor.

This change replaces both hardcoded registries with a single JSON-driven source of truth, folds `effects_factory.py` into `effects_loader.py` (one cohesive module), and deletes `_DEFAULT_EFFECTS_LIST_FULL` outright. The JSON schema mirrors the `EffectsSettings` dataclass shape — every field on the class is a field on the JSON, with `module` + `class_name` added to each effect dict so the factory can resolve them dynamically. Pi-local development becomes a JSON edit against a gitignored override file in `config_overrides/effects_settings.json` — one file, one env var, one wire-strip rule.

## Goals / Non-Goals

**Goals:**
- Single declarative source of truth that mirrors the `EffectsSettings` dataclass shape (one JSON file, not two).
- Factory + Flask admin UI read the same source.
- Pi-local development via a gitignored override file in a dedicated folder.
- Override-active state controls whether the Pi accepts the wire's `effects_settings` block.
- Wire shape is preserved (no breaking change to the MQTT contract).
- Behavior-preserving refactor for operators who don't create the override file.

**Non-Goals:**
- Override files for other configs (`config.json` for text_settings / filters / senders, `seed_messages.json`) — separate change.
- Full "local dev mode" where all external inputs are stubbed — separate change.
- Plugin/decorator discovery system — explicitly rejected.
- MERGE semantics for the override — REPLACE-only for this change.
- Per-effect metadata beyond what the `EffectsSettings` class needs (description, category, requires_assets, asset_paths, default_enabled) — none of these belong on the wire or in the JSON; metadata is documentation, not config.

## Decisions

### D1. Override location: `config_overrides/` folder, not repo-root files

**Choice:** All override files live in a single `config_overrides/` folder at the repo root. The folder is gitignored as a single entry. The first override file is `config_overrides/effects_settings.json`. Future overrides (`config.json`, `seed_messages.json`) follow the same pattern.

**Rationale:** One gitignore line covers all current and future override files. The folder is discoverable in the editor (sits at the repo root, alongside the existing `docs/`, `scripts/`, `lib_shared/` directories). Operator workflow is consistent: "any local override goes in `config_overrides/`."

**Alternatives considered:**
- *Override files at the repo root* (e.g., `effects_settings_override.json`): requires a separate gitignore line per future override, and the root gets cluttered.
- *Override files in `lib_shared/`* (e.g., `lib_shared/effects_settings_override.json`): mixes "in-git source" with "out-of-git local" in the same directory — confusing.
- *System-level `/etc/lindsay-50/`*: correct for the longer-term vision, but the operator loses the "right in the editor" affordance. The `EFFECTS_SETTINGS_OVERRIDE` env var preserves this option for advanced operators.

### D2. Override semantics: REPLACE only

**Choice:** If the override file exists, the loader uses it INSTEAD OF the canonical. The operator must copy the canonical, modify, save the whole file. No merge.

**Rationale:** Simpler loader code, simpler mental model. The JSON is small enough (~30 lines for the canonical) that the operator copy-pastes the whole file — cheap. Merge semantics add edge cases (what if the override has a subset of effect names? what if it has additional names? what if a field is omitted?).

**Trade-off:** When the canonical gains a new effect, the operator's override doesn't see it until they re-copy. Acceptable for the current scale.

### D3. Override path precedence: env var > gitignored folder > canonical

**Choice:** The loader checks in this order:
1. `EFFECTS_SETTINGS_OVERRIDE` env var, if set and the file exists.
2. `config_overrides/effects_settings.json` at the repo root, if it exists.
3. `lib_shared/config/effects_settings.json` (canonical, in git).

**Rationale:** Env var is primarily a test-fixture hook — pytest fixtures can point each test at a synthetic JSON path without polluting the repo. It's also available to operators who want to point at a non-standard location, but the common case is the repo-root file. Canonical is the always-works fallback.

### D4. JSON schema mirrors `EffectsSettings` dataclass shape

**Choice:** The canonical and override `effects_settings.json` files declare exactly the fields the `EffectsSettings` dataclass accepts: `effects`, `fade_seconds`, `hold_seconds`, `intro_seconds`, `idle_seconds`, `recent_count`, plus a top-level `schema_version` for the loader's migration policy. Each entry in the `effects` list carries `name`, `enabled`, `module`, `class_name` — the first two are the existing wire shape, the latter two are the registration metadata the factory needs to dynamic-import the class.

**Rationale:** `EffectsSettings` is the canonical type. Making the JSON match its shape means:
- `EffectsSettings.from_dict(json.load(f))` is a near-direct mapping (only the `module` / `class_name` fields are extra, and they're stripped before constructing the dataclass).
- The Flask admin UI's wire payload and the JSON file share the same shape; no shape translation layer.
- No invented metadata (description, category, default_enabled, requires_assets, asset_paths) that lives in JSON but has no consumer.

**Trade-off:** The `EffectsSettings` dataclass stays coupled to the JSON shape. If the class gains a field, the JSON gains a field. Acceptable — they're the same conceptual thing.

### D5. `enabled` (not `default_enabled`) is the per-effect state

**Choice:** Each effect entry carries `enabled: bool`, not `default_enabled`. The JSON IS the current state.

**Rationale:** The previous design carried `default_enabled: true` for Fireworks/Flame/NightSky/Honeycomb/Hyperspace and `default_enabled: false` for PngDisplay/VideoDisplay — the `default_` prefix implied a separate runtime override that never materialized. The wire shape today carries `enabled` (no prefix), and the Flask admin UI flips it. The JSON should match the wire: just `enabled`. The asset-dependent effects (PngDisplay, VideoDisplay) ship with `enabled: false` in the canonical; operators flip to `true` once they've populated `design/pngs/` / `design/videos/`.

### D6. Wire shape: unchanged

**Choice:** The Flask admin still publishes `effects_settings = {"effects": [{"name": "X", "enabled": true}, ...], "fade_seconds": ..., ...}` over MQTT. The Pi's manager still receives it. The only difference: when the override is active, the manager strips the entire `effects_settings` block before applying, so the local JSON is authoritative.

**Rationale:** Preserves the wire contract. Any tool that already speaks the protocol keeps working. The change is purely in how the Pi handles the block when the override is active.

### D7. Override-active wire-strip: drop the entire `effects_settings` block

**Choice:** When `is_effects_settings_override_active()` is True, the Pi's `MessageManager._handle_config` pops `payload["effects_settings"]` if present. Top-level `payload["text_settings"]` and `payload["filters"]` still come from the wire (they're owned by future `config.json` overrides, out of scope here).

**Rationale:** The override owns ALL of `EffectsSettings` — both the effects list AND the timing fields. There's no "Pi owns the effects list but Flask owns the pacing" mode; that's the wrong mental model. Per-file ownership means: "if my override is active, I own everything in `EffectsSettings`; the rest comes from Flask."

### D8. DELETE `_DEFAULT_EFFECTS_LIST_FULL` outright; consumers use loader functions

**Choice:** `lib_shared/models.py::_DEFAULT_EFFECTS_LIST_FULL` is REMOVED entirely. No backward-compat alias, no derived property, no module-`__getattr__` shim. The 3 test files that previously monkey-patched it (`tests/test_boot_config_endpoint.py`, `tests/test_auth.py`, `tests/test_sign_status_endpoint.py`) get rewritten to inject loader fixtures via `effects_loader.reset_effects_settings()` after setting a fake config dict. `EffectsSettings.__init__` reads its defaults from `load_effects_settings()` on construction (the dataclass field defaults stay as fallbacks for the no-loader-import case).

**Rationale:** A transitional alias would outlive its purpose indefinitely. Tests that monkey-patched the literal were testing the wrong thing — the source of truth is the JSON, not a module-level re-export. Removing the alias forces every reader to call `load_effects_settings()` directly, which makes test isolation straightforward and the call graph honest.

**Trade-off:** Larger blast radius on the 3 test files (rewrites vs. one-line patches). Worth it for the cleaner architecture.

### D9. Loader cache: process-lifetime, with `reset_effects_settings()` for tests

**Choice:** `load_effects_settings()` caches the parsed dict for the process lifetime. The first call reads the file; subsequent calls return the cached value. A `reset_effects_settings()` function is exposed for tests that need to swap configs.

**Rationale:** The config doesn't change at runtime — the Pi reads at boot, the Flask app reads at startup. No hot-reload needed.

### D10. Schema-version mismatch: fail loudly for explicit mismatches, silent fallback for parse errors

**Choice:** The loader logs the file's `schema_version` field.
- If the file's version > the loader's known max: fail loudly (operator has a future-version file).
- If the file's version < the loader's known min: log a warning, attempt to load (best-effort).
- If the file fails to parse: log error, fall back to canonical (file is just garbled).

**Rationale:** The fail-loudly case is "operator clearly wrote the wrong file" — better to refuse to start than to silently behave incorrectly. The best-effort case is "loader is newer than the file" — the operator may need time to update. The silent-fallback case is "file is corrupt" — the sign should keep working on the canonical.

### D11. Empty `effects` list: WARNING log, not fatal

**Choice:** If the loaded config has an empty `effects` list, the loader logs a WARNING at boot and returns the empty list as-is. The coordinator's `build_effects()` falls back to the first canonical effect per its existing contract, so the sign never goes dark. No `assert` — loading with zero effects is a valid debugging state.

**Rationale:** Forcing a fatal exit on an empty list blocks legitimate debug scenarios (e.g. an operator testing rotation behavior with an empty override, or running the Pi locally without any effects loaded). The WARNING is loud enough to surface in `journalctl`; the `build_effects()` fallback ensures the sign still produces a frame.

### D12. Env var has no `LINDSAY50_` prefix

**Choice:** The env var is `EFFECTS_SETTINGS_OVERRIDE`, not `LINDSAY50_EFFECTS_SETTINGS_OVERRIDE`.

**Rationale:** The `LINDSAY50_` prefix is reserved for project-level process-control env vars (`LINDSAY50_ACTIVE_SHA`, `LINDSAY50_REPO_DIR`) — vars that identify the deployment itself. Config-overrides are operator-environment concerns and live in the operator's shell, not the loader's process identity; a shorter unprefixed name is the right scope.

## Example Files

### `lib_shared/config/effects_settings.json` (canonical)

```json
{
  "schema_version": 1,
  "effects": [
    {"name": "Hyperspace", "module": "lib_shared.patterns.hyperspace", "class_name": "Hyperspace", "enabled": true},
    {"name": "VideoDisplay", "module": "lib_shared.patterns.video_display", "class_name": "VideoDisplay", "enabled": false},
    {"name": "PngDisplay", "module": "lib_shared.patterns.png_display", "class_name": "PngDisplay", "enabled": false},
    {"name": "Honeycomb", "module": "lib_shared.patterns.honeycomb", "class_name": "Honeycomb", "enabled": true},
    {"name": "Flame", "module": "lib_shared.patterns.flame", "class_name": "Flame", "enabled": true},
    {"name": "Fireworks", "module": "lib_shared.patterns.fireworks", "class_name": "Fireworks", "enabled": true},
    {"name": "NightSky", "module": "lib_shared.patterns.nightsky", "class_name": "NightSky", "enabled": true}
  ],
  "fade_seconds": 2.0,
  "hold_seconds": 15.0,
  "intro_seconds": 5.0,
  "idle_seconds": 300.0,
  "recent_count": 5
}
```

### `config_overrides/effects_settings.json` (Pi-local override, gitignored)

```json
{
  "schema_version": 1,
  "effects": [
    {"name": "Fireworks", "module": "lib_shared.patterns.fireworks", "class_name": "Fireworks", "enabled": true},
    {"name": "NightSky", "module": "lib_shared.patterns.nightsky", "class_name": "NightSky", "enabled": true},
    {"name": "PngDisplay", "module": "lib_shared.patterns.png_display", "class_name": "PngDisplay", "enabled": true},
    {"name": "Flame", "module": "lib_shared.patterns.flame", "class_name": "Flame", "enabled": false}
  ],
  "fade_seconds": 1.0,
  "hold_seconds": 8.0,
  "intro_seconds": 3.0,
  "idle_seconds": 60.0,
  "recent_count": 3
}
```

## Risks / Trade-offs

- **[Risk] Reverting the recipe's `local_effects` kwarg breaks any in-flight Pi-local development** → Mitigation: the recipe was merged recently and only used by dev Pi operators experimenting with new patterns. No production deployments rely on the kwarg. Operators running local patterns at the time of the revert will see their patterns stop appearing until they move to the new JSON-driven mechanism (covered in the rewritten `docs/adding-patterns.md`).
- **[Risk] Operator forgets to re-copy the canonical after upgrading, missing new effects** → Mitigation: log at boot which file is in use and how many effects it contains. Visible in `journalctl`.
- **[Risk] Override file parse error falls back silently, surprising the operator** → Mitigation: log error at WARNING level. The boot log clearly shows "loaded N effects from /path/to/source".
- **[Risk] Dynamic import at factory-call time adds latency on first invocation** → Mitigation: trivial — Python imports are cached after first call, and the factory is invoked at coordinator boot, not in the hot tick path.
- **[Risk] The `local_effects` kwarg revert breaks callers we don't know about** → Mitigation: the kwarg was added by the recipe, merged recently. No other callers in the codebase (grep `local_effects` confirms). The Pi's `main.py` is the only call site; it's reverted in the same change.
- **[Risk] The Flask admin UI template change renders differently for users with the override vs. the canonical** → Mitigation: both branches read `effects_settings` from the loader, so the template logic is identical. Only the source file differs.
- **[Risk] JSON field names drift from `EffectsSettings` dataclass** → Mitigation: the dataclass IS the schema; any field added to the dataclass must be added to the JSON, and vice versa. A unit test asserts `set(EffectsSettings.__init__.__defaults__.keys()) ⊆ set(canonical_json.keys())` to catch drift.

## Migration Plan

1. Land the change as a single PR. The order of commits within the PR:
   - Commit 1: `lib_shared/config/effects_settings.json` (under the new `lib_shared/config/` folder) + `lib_shared/effects_loader.py` + factory refactor. Behavior unchanged. Pi and Flask both still resolve the canonical 7 effects.
   - Commit 2: Flask admin UI templates + `_KNOWN_EFFECT_NAMES` derivation. Flask side is now config-driven.
   - Commit 3: REVERT recipe's `local_effects` additions. `lib_shared/effects_coordinator.py` removes the 5 added lines (`__init__`, `bind`, `_tick_inner` re-append branch). `heart-matrix-controller/main.py` removes the kwarg at the `EffectsCoordinator(...)` construction.
   - Commit 4: `lib_shared/message_manager.py` strips wire `effects_settings` when override is active. `EffectsSettings.__init__` reads from `load_effects_settings()`. `.gitignore` adds `config_overrides/`.
   - Commit 5: Test updates + `docs/adding-patterns.md` rewrite.
2. Rollback: `git revert <merge-sha>`. The change is fully revertible — no schema migrations, no data migrations, no wire-format changes.

## Open Questions

1. **Should the canonical ship `PngDisplay` / `VideoDisplay` with `enabled: false`?** RESOLVED — see D5. Yes, both included with `enabled: false` so the Flask admin UI surfaces the toggle surface while the rotation is safe on a fresh checkout.
2. **Should the override file support merge semantics in the future?** RESOLVED — no, never. REPLACE-only is the entire point of decoupling from Flask (D2).
3. **What happens if the canonical is missing or corrupt on a fresh checkout?** RESOLVED — see D11. WARNING log, not fatal. The coordinator's `build_effects()` fallback keeps the sign producing a frame.
4. **Should the env var have a `LINDSAY50_` prefix?** RESOLVED — see D12. No; that prefix is reserved for project-level process-control env vars.

(No forward-looking open questions remain — all four are resolved above.)