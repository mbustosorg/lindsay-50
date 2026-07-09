# Tasks: effects-override

## 1. Canonical effects_settings + loader

- [ ] 1.1 Create `lib_shared/config/effects_settings.json` (under a new `lib_shared/config/` folder that will hold all in-git config JSON files) declaring the canonical `EffectsSettings` block. Schema mirrors the `EffectsSettings` dataclass exactly: `effects` (list of `{name, enabled, module, class_name}` dicts), `fade_seconds`, `hold_seconds`, `intro_seconds`, `idle_seconds`, `recent_count`, plus top-level `schema_version`. All 7 effects included in canonical order (Hyperspace, VideoDisplay, PngDisplay, Honeycomb, Flame, Fireworks, NightSky) with `enabled: true` for the 5 ambient patterns and `enabled: false` for the 2 asset-dependent ones.
- [ ] 1.2 Create `lib_shared/effects_loader.py` with `load_effects_settings()`, `is_effects_settings_override_active()`, and `reset_effects_settings()`. Precedence: `EFFECTS_SETTINGS_OVERRIDE` env var > `config_overrides/effects_settings.json` > canonical. Log source path + count on first load. Schema-version mismatch policy per design D10. Empty `effects` list yields WARNING log + return as-is (design D11) — no fatal assert.
- [ ] 1.3 DELETE `lib_shared/effects_factory.py`. The `make_effect_class()` function moves into `lib_shared/effects_loader.py`. Implementation in the loader: look up `name` in `load_effects_settings()["effects"]`, `importlib.import_module(entry["module"])`, `getattr(module, entry["class_name"])`, return the class. Preserve on-demand import (no numpy/cv2/PIL at module load). The factory is now a function in the loader module — no separate `effects_factory.py` file.
- [ ] 1.4 Add tests in `tests/effects_loader_test.py` — env var precedence, repo-root override, canonical fallback, override-missing-file fallback, schema validation (every effect entry has `name`, `enabled`, `module`, `class_name`; the top-level keys are an exact subset of the `EffectsSettings` dataclass fields plus `schema_version`), `reset_effects_settings()` roundtrip, AND the folded-in factory tests (`make_effect_class` resolves each canonical name, returns `None` for unknown names, raises `AttributeError` for wrong class names). The factory tests previously lived in `tests/effects_factory_test.py`; that file is deleted as part of 1.3 and its cases move here. Add an empty-`effects`-list case: when canonical has `{"effects": []}`, the loader returns `[]` with a WARNING (not an exception).

## 2. Flask admin UI derives from config

- [ ] 2.1 In `heart-message-manager/main.py`, replace the hardcoded `_KNOWN_EFFECT_NAMES` frozenset with a module-level call to `load_effects_settings()["effects"]` mapped to `{entry["name"] for entry in entries}`. Log the count at startup.
- [ ] 2.2 In the `/settings` route handler, pass `effects_settings=load_effects_settings()` to the template context.
- [ ] 2.3 In the `/playful/settings` route handler, do the same.
- [ ] 2.4 Update `heart-message-manager/templates/settings.html` — iterate `effects_settings.effects` for the per-effect rows (name + enabled toggle + module/class_name rendered as a small caption for debugging). Top-of-form `fade_seconds` / `hold_seconds` / `intro_seconds` / `idle_seconds` / `recent_count` inputs read from `effects_settings` (falling back to `cfg.effects_settings.*` once a wire envelope has arrived).
- [ ] 2.5 Update `heart-message-manager/templates/playful/settings-playful.html` to match.
- [ ] 2.6 Add tests in `tests/test_admin_settings_route.py` — admin renders effects from config; an effect name added at runtime via override (with `reset_effects_settings()`) shows up; deleted canonical name does NOT show up; the timing fields pre-populate from canonical `effects_settings` when no wire envelope is present.

## 3. REVERT recipe's `local_effects` additions

The recipe PR added a `local_effects` kwarg to `EffectsCoordinator` and a `local_effects=[...]` arg in `main.py`. The user has rejected this transitional mechanism; this task group reverts those additions as part of the broader JSON-driven refactor.

- [ ] 3.1 In `lib_shared/effects_coordinator.py`, REVERT the recipe's additions: remove the `local_effects` parameter from `EffectsCoordinator.__init__` and `bind()`, remove the `_local_effects` attribute, remove the re-append branch in `_tick_inner`'s structural-diff rebuild. Net diff: ~5 lines removed.
- [ ] 3.2 In `heart-matrix-controller/main.py` (currently line 136), REVERT the recipe's `local_effects=[...]` kwarg from the `EffectsCoordinator(...)` construction site. Net diff: 1 line removed.
- [ ] 3.3 Verify no other call sites construct `EffectsCoordinator` (grep `EffectsCoordinator(` across `lib_shared/`, `heart-message-manager/`, `heart-matrix-controller/`). Remove any test that passed `local_effects`.
- [ ] 3.4 Update `tests/effects_coordinator_test.py` — remove any `local_effects=[...]` test fixtures. Confirm the existing rotation-rebuild tests still pass without the re-append branch.

## 4. Wire-strip on override + `EffectsSettings` reads from loader + .gitignore

- [ ] 4.1 In `lib_shared/message_manager.py::_handle_config`, before calling `SignConfig.update_from_dict(payload)`: if `is_effects_settings_override_active()`, pop `payload["effects_settings"]` if present. Log at DEBUG when this happens. Top-level `text_settings` and `filters` still come from the wire as normal.
- [ ] 4.2 In `lib_shared/models.py`, DELETE `_DEFAULT_EFFECTS_LIST_FULL` outright. No alias, no property, no module-`__getattr__` shim. Confirm via `grep -rn '_DEFAULT_EFFECTS_LIST_FULL' lib_shared/` that no remaining reference exists in the source tree. Modify `EffectsSettings.__init__` to call `load_effects_settings()` for default values when no argument is passed: the dataclass field defaults stay as fallbacks for the no-loader case, but the JSON-derived values win when present (only the keys the JSON declares are populated; the dataclass defaults fill any gaps).
- [ ] 4.3 Add `config_overrides/` to `.gitignore` as a single trailing-slash entry.
- [ ] 4.4 Add tests in `tests/message_manager_test.py` — when override active, `effects_settings` is stripped; when override not active, `effects_settings` is preserved; `text_settings` and `filters` always pass through.
- [ ] 4.5 Rewrite the 3 test files that previously monkey-patched `_DEFAULT_EFFECTS_LIST_FULL` (`tests/test_boot_config_endpoint.py`, `tests/test_auth.py`, `tests/test_sign_status_endpoint.py`). Each test that needed an empty / custom effects list now sets a fake config and calls `effects_loader.reset_effects_settings()` (or uses a test fixture that does the same). The monkey-patches are gone — there is no alias to patch.

## 5. Docs and final verification

- [ ] 5.1 Rewrite `docs/adding-patterns.md` for the new mechanism. Drop the `local_effects` recipe. New TL;DR: copy `lib_shared/config/effects_settings.json` to `config_overrides/effects_settings.json`, add or toggle entries (or adjust `fade_seconds` / `hold_seconds` / etc.), save. Show the 30-second Pi test command (`sudo PYTHONPATH=.. LOG_LEVEL=INFO python3 main.py` from `heart-matrix-controller/`). Cover the "promote to canonical" footnote (move the entry into `lib_shared/config/effects_settings.json` in git, drop the override).
- [ ] 5.2 Run `PYTHONPATH=. pytest tests/ -v` — all tests green.
- [ ] 5.3 Run `PYTHONPATH=. python -c "from lib_shared.effects_loader import make_effect_class; print(make_effect_class('Fireworks'))"` — returns the class. (`effects_factory` no longer exists; the factory lives in the loader module.)
- [ ] 5.4 Run `PYTHONPATH=. python -c "from lib_shared.effects_loader import load_effects_settings, is_effects_settings_override_active; print(is_effects_settings_override_active(), load_effects_settings()['recent_count'])"` — outputs `False 5` on a fresh checkout with no override.
- [ ] 5.5 Create an override file at `config_overrides/effects_settings.json` with 3 entries and `recent_count: 3`; re-run the same loader probe — outputs `True 3`.
- [ ] 5.6 `git diff --stat` shows expected file list. `git status` shows `config_overrides/` as ignored.
- [ ] 5.7 Commit per the design's 5-commit migration plan. Open PR.