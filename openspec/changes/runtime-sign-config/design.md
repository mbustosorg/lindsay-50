## Context

The sign's runtime behavior is hard-coded. `EffectsCoordinator` is constructed in `heart-matrix-controller/main.py:55-61` with all defaults (no overrides), and the effect list passed in — `[hyperspace, video, png, honeycomb, flame, fireworks, nightsky]` — is a literal in the same file. There is no path for an operator to disable `video_display` (which depends on a video file the operator may not have), reorder the rotation, change the hold time, or change the `recent_count` size — without SSH + edit + reboot.

`SignConfig` already round-trips through Flask → SQLite → S3 → MQTT → `MessageManager._handle_config` (and the browser preview's `MessageManager` reads it on seed). The wire path works. What's missing is the **fields that change behavior** — the schema has `sign`, `filters`, `senders`, `timezone` (and the leftover `tz_offset_mins`); the old `rendering` block is going away (its text-color field moves to the new `text_settings` block). What's NOT there yet is an `effect_settings` block and a `text_settings` block. The Flask `PUT /api/config` endpoint accepts whatever is in the body; `SignConfig.from_dict` drops unknown keys. The Pi's coordinator never reads any of this — it builds the effect list and coordinator from the hard-coded literals in `main.py`.

The settings page (`heart-message-manager/templates/settings.html`) shows form fields that look like they're configuring the sign, but `rendering.color` and `rendering.speed` are stored and round-tripped but not used by the device. Operators have no way to know that.

This change closes the loop: add the missing blocks to `SignConfig`, validate them in the Flask endpoint, surface them in the admin UI, and consume them on the Pi (and in the browser preview) when the coordinator and scroller are constructed. While we're here, remove the two CircuitPython-era leftovers (`tz_offset_mins`, the `EffectsCoordinator.request_message` indirection), and add a small `MIGRATIONS` registry so the breaking config-shape change is transparent to existing stored configs in S3 / SQLite.

**Why not just edit `main.py` on the Pi?** That's the current state. It works but it's a redeploy, a service restart, and a chicken-and-egg with the operator's ability to preview changes. The preview already runs the same effect code in the browser; once the config is the source of truth, the preview gets the new fields for free and operators can verify "the sign will do this" before saving.

## Goals / Non-Goals

**Goals:**

- An operator can change the rotation, the pacing (fade / hold / intro / idle / recent), and the scroll speed from the admin UI, save, and the sign updates within one MQTT round trip — no SSH, no redeploy, no Pi reboot.
`settings.toml` is **not** updated for this change. The defaults for `effect_settings` and `text_settings` come from the `EffectsSettings()` and `TextSettings()` constructor defaults and the `_DEFAULT_EFFECTS_LIST_FULL` constant in `lib_shared/models.py`. The hard-coded defaults are reviewed alongside the effect class definitions in `heart-matrix-controller/main.py` (and the `_EFFECT_CLASSES` map).
- The settings page surfaces the controls that map to **pacing and rotation behavior**, not to the technical parameters that operators don't need to reason about (`fade_step`, `gamma` stay as code defaults). The issue says "the goal isn't fine-tuning" — but the operator reviewed the design and explicitly asked to surface `recent_count` (it controls the idle rotation pool size, which is the kind of knob operators want). We surface it; the rest stays as code defaults.
- A `text_effect` field exists in the config (default `"scroll"`) so future text effects (swirl, bounce) can be added without a schema change. The scroller doesn't branch on it in v1; the field is plumbed end-to-end so the next change is a one-line addition.
- The wire path is unchanged (`type="config"` `MessageEnvelope`, `SignConfig.from_dict` / `to_dict`). The change is purely additive fields plus the `tz_offset_mins` removal.
- The same configuration drives the Pi and the browser preview. Same fields, same validation, same default values.
- Existing stored `version: 1` configs in S3 and SQLite are preserved across the upgrade (the v1 → v2 migration preserves `filters`, `senders`, `sign`, `rendering`, `timezone`; drops the now-redundant `tz_offset_mins`; adds the new blocks with defaults; bumps the version).

**Non-Goals:**

- Wi-Fi credential management (deferred to a follow-up change — see the Open Questions).
- Per-effect arguments (color palette swaps, firework density, etc.) — the issue explicitly says "do not surface arguments for each specific effect."
- Refactoring `ScrollerBase` or its subclasses — the issue says "we don't need to refactor ScrollerBase or its children at this point."
- A separate "preset" / "theme" abstraction — the config is the config.
- Changing the rendering speed wire shape (the v1 `RenderingSettings.speed` and `color` fields are removed; the new `TextSettings` block is the home for text rendering parameters, starting fresh with the scroller's defaults and a `text_effect` field for future variants).
- Changing the way the `MessageEnvelope` is parsed or the way config messages are dispatched (`MessageManager._handle_config` stays as-is; it just calls `update_from_dict` on the new shape, and the migration runs inside `update_from_dict`).
- A full ORM migration framework (Alembic, Django migrations, Pydantic model validators, etc.) — for a project with one config blob, a small `MIGRATIONS` registry is the right shape. See Decision 11.

## Decisions

### Decision 1: Two new top-level fields on `SignConfig` (`effect_settings` + `text_settings`); `rendering` removed

`SignConfig` gains `effect_settings: EffectsSettings` and `text_settings: TextSettings`. The existing `filters`, `senders`, `sign`, and `timezone` fields stay put. **`rendering` is REMOVED entirely** — the old `RenderingSettings` block (with `speed`, `color`, `mode`) is being replaced by the new `text_settings` block. The old v1 field was a thin wrapper around form values that the device never actually consumed (the device used its own scroller constructor defaults); the new `text_settings` block is what the device actually reads. `tz_offset_mins` is also removed. The `version` field stays (and the default bumps from 1 to 2 — see Decision 2). The text field is named `text_settings` (not `scroller_settings` or `scroller`) because the scroller is just one text effect — future text effects (swirl, bounce) will share the same block and may use different fields. Naming the block after "text" (the general concept) instead of "scroller" (one implementation) leaves room for those.

**`EffectsSettings`, `TextSettings`, and `_DEFAULT_EFFECTS_LIST_FULL` all live in `lib_shared/models.py`** — alongside `SignConfig`. The classes are small (~20 lines each), they share concepts (defaults, `from_dict` / `to_dict`, the `_DEFAULT_EFFECTS_LIST_FULL` constant), and a single file is the right shape for "a config struct and the two nested settings blocks it carries." Splitting them into separate files would mean three files to edit for any cross-cutting change and would make the test imports noisier. The exception is the migration registry, which is a separate concern (chained forward-only transforms) and stays in `lib_shared/config_migrations.py`. The old `RenderingSettings` class is **deleted** from `lib_shared/models.py` — its only use site was `SignConfig.rendering`, which is also being removed.

**`EffectsSettings` groups everything the `EffectsCoordinator` consumes:**

```python
class EffectsSettings:
    def __init__(
        self,
        effects: list[dict] | None = None,    # [{"name": str, "enabled": bool}, ...]
        fade_seconds: float = 2.0,
        hold_seconds: float = 15.0,
        intro_seconds: float = 5.0,
        idle_seconds: float = 300.0,
        recent_count: int = 5,
    ):
        self.effects = list(effects) if effects is not None else list(_DEFAULT_EFFECTS_LIST_FULL)
        self.fade_seconds = fade_seconds
        self.hold_seconds = hold_seconds
        self.intro_seconds = intro_seconds
        self.idle_seconds = idle_seconds
        self.recent_count = recent_count

    @classmethod
    def from_dict(cls, d):
        d = d or {}
        effects = d.get("effects", _DEFAULT_EFFECTS_LIST_FULL)
        if not isinstance(effects, list) or not all(
            isinstance(n, dict) and isinstance(n.get("name"), str) and isinstance(n.get("enabled"), bool)
            for n in effects
        ):
            raise ValueError("effects must be a list of {name: str, enabled: bool} objects")
        return cls(
            effects=[{"name": n["name"], "enabled": n["enabled"]} for n in effects],
            fade_seconds=float(d.get("fade_seconds", 2.0)),
            hold_seconds=float(d.get("hold_seconds", 15.0)),
            intro_seconds=float(d.get("intro_seconds", 5.0)),
            idle_seconds=float(d.get("idle_seconds", 300.0)),
            recent_count=int(d.get("recent_count", 5)),
        )

    def to_dict(self):
        return {
            "effects": self.effects,
            "fade_seconds": self.fade_seconds,
            "hold_seconds": self.hold_seconds,
            "intro_seconds": self.intro_seconds,
            "idle_seconds": self.idle_seconds,
            "recent_count": self.recent_count,
        }

    def validate(self):
        if self.fade_seconds < 0 or self.hold_seconds < 0 \
                or self.intro_seconds < 0 or self.idle_seconds < 0:
            raise ValueError("pacing durations must be non-negative")
        if self.recent_count < 1:
            raise ValueError("recent_count must be a positive integer")
```

`_DEFAULT_EFFECTS_LIST_FULL` is the full canonical 7-entry list of `{"name", "enabled"}` objects, in the canonical rotation order:

```python
_DEFAULT_EFFECTS_LIST_FULL = [
    {"name": "Hyperspace",   "enabled": True},
    {"name": "VideoDisplay", "enabled": False},  # needs asset files
    {"name": "PngDisplay",   "enabled": False},  # needs asset files
    {"name": "Honeycomb",    "enabled": True},
    {"name": "Flame",        "enabled": True},
    {"name": "Fireworks",    "enabled": True},
    {"name": "NightSky",     "enabled": True},
]
```

The 5 historically-defaulted effects are `enabled: true`; `VideoDisplay` and `PngDisplay` are present but `enabled: false` because they depend on operator-supplied asset files. The full set is still visible to the operator (the UI renders all 7 entries), and toggling one to `enabled: true` adds it to the rotation — the device already skips effects whose constructor raises (the WARN-and-skip path), so even if the asset files are missing, the device boots and runs. The constant lives in `lib_shared/models.py` (not a separate `effects_settings.py`) so it's shared between the Pi and the Flask process without an extra import.

**`TextSettings` lives in `lib_shared/models.py`**:

```python
class TextSettings:
    # v1 supports "scroll" only; more values land as future text effects.
    TEXT_EFFECTS = ("scroll",)  # type: tuple[str, ...]

    def __init__(
        self,
        frame_delay: float = 0.04,
        offset_seconds: float = 1.0,
        color: int = 0xFF0000,
        text_effect: str = "scroll",
    ):
        self.frame_delay = frame_delay
        self.offset_seconds = offset_seconds
        self.color = color
        self.text_effect = text_effect

    @classmethod
    def from_dict(cls, d):
        d = d or {}
        text_effect = d.get("text_effect", "scroll")
        if text_effect not in cls.TEXT_EFFECTS:
            raise ValueError(f"text_effect must be one of {cls.TEXT_EFFECTS}, got {text_effect!r}")
        return cls(
            frame_delay=float(d.get("frame_delay", 0.04)),
            offset_seconds=float(d.get("offset_seconds", 1.0)),
            color=int(d.get("color", 0xFF0000)),
            text_effect=text_effect,
        )

    def to_dict(self):
        return {
            "frame_delay": self.frame_delay,
            "offset_seconds": self.offset_seconds,
            "color": self.color,
            "text_effect": self.text_effect,
        }
```

**`SignConfig` (in `lib_shared/models.py`) constructor signature:**

```python
CURRENT_VERSION = 2

def __init__(
    self,
    filters=None,
    senders=None,
    rendering=None,
    sign=None,
    timezone="US/Pacific",
    version=CURRENT_VERSION,
    effect_settings=None,                   # NEW
    text_settings=None,                      # NEW (replaces the old rendering block)
    allowed_senders=None,
):
    self.filters = filters or []
    self.senders = senders or {}
    self.sign = sign if isinstance(sign, SignSettings) else SignSettings.from_dict(sign or {})
    self.timezone = timezone
    self.version = version
    self.effect_settings = (
        effect_settings if isinstance(effect_settings, EffectsSettings)
        else EffectsSettings.from_dict(effect_settings or {})
    )
    self.text_settings = (
        text_settings if isinstance(text_settings, TextSettings)
        else TextSettings.from_dict(text_settings or {})
    )
    self._lock = threading.RLock()
```

`from_dict`, `to_dict`, `update`, and `update_from_dict` add the two new keys (named `effect_settings` and `text_settings` on the wire). `tz_offset_mins` and `rendering` are removed from all four. The same `threading.RLock` guards mutations. The `migrate(...)` call is added at the top of `from_dict` and `update_from_dict` (see Decision 11).

**Why a single `effect_settings` block and not separate `effects` + `pacing` top-level fields?** The coordinator consumes both — pairing them in one object means the coordinator's constructor takes one focused argument (`EffectsSettings`), not "the whole `SignConfig`." Future additions to the coordinator (e.g. a `transition` mode, an `intro_splash` toggle, a `boot_splash_enabled` flag) land in the same block without growing `SignConfig`'s top-level surface, and the coordinator's narrower knowledge (no filters, no sign name, no timezone) is a real win — it's the same shape that lets a future `TextSettings`-like block be consumed by a different class without bleeding the rest of `SignConfig` in.

**Why not nest under a `runtime` block (effects + pacing + scroller all under one)?** The two blocks have different consumers (the coordinator vs. the scroller) and different lifecycles. The coordinator is rebuilt on every config message; the scroller is constructed once at boot. Two top-level blocks keep the separation clear and make it obvious which code reads which block.

**Why the rename from `PacingSettings` to `EffectsSettings`?** The block holds both the effects list (which is rotation-specific, not pacing) and the timing params; the new name reflects the broader scope. Future additions to the coordinator (transition modes, etc.) also fit cleanly under "Effects" — the block is the "everything the EffectsCoordinator needs" bag.

### Decision 2: Schema version bumps to 2; `tz_offset_mins` is removed (not deprecated)

The existing config has `version: 1`. The new shape is a strict superset (adds blocks, removes one field). The `version` default bumps from 1 to 2. Existing stored `version: 1` payloads are transparently upgraded on read by the migration registry (Decision 11).

**Why is `tz_offset_mins` removed, not deprecated?** It was a CircuitPython workaround. The Pi now uses `zoneinfo.ZoneInfo`, which gives a correct offset for any IANA timezone (including DST). The offset is computed at read-time by `lib_shared/messages._format_display_time` from the `timezone` field. Storing a recomputed offset in the config and threading it through every read site is a vestige; removing it deletes the recompute on every config write (`heart-message-manager/sqlite.py:137-139`) and the `tz_offset_mins` parameter on `_format_display_time` (`lib_shared/messages.py:16`).

The wire shape change is: `SignConfig.to_dict()` no longer emits `tz_offset_mins`. The Flask endpoint's `PUT /api/config` accepts payloads without it; the device's `MessageManager._handle_config` calls `update_from_dict`, which runs `migrate(...)` and then ignores the absent key.

**Migration for old stored configs:** the v1 → v2 migration (registered in `MIGRATIONS` in `lib_shared/config_migrations.py`) does the upgrade on read. **Critically, the server also runs the migration on startup** (see Decision 11b): after the existing "rebuild-from-S3 on startup" step reads the latest config from S3, the server runs `migrate(...)` on it, and if a migration ran, writes a new S3 entry, updates the local SQLite cache, and publishes a `type="config"` envelope to MQTT. The point is to avoid months of backward-compatibility — the running code only ever sees the current version. The migration preserves `filters`, `senders`, `sign`, `timezone`; **drops** `tz_offset_mins` AND `rendering` (the old `RenderingSettings` block is being removed; the new `text_settings` block replaces it); adds the two new blocks (`effect_settings` and `text_settings`) with their defaults; bumps the version. Messages stored in S3 (`messages.json`) are not part of the config; the migration doesn't touch them. Suppression rules (in `filters`) are preserved as-is.

### Decision 3: Defaults live in code, not in `settings.toml`

`settings.toml` is **not** updated for this change. The defaults for `effect_settings` and `text_settings` come from the `EffectsSettings()` and `TextSettings()` constructor defaults and the `_DEFAULT_EFFECTS_LIST_FULL` constant in `lib_shared/models.py`. The hard-coded defaults are reviewed alongside the effect class definitions in `heart-matrix-controller/main.py` (and the `_EFFECT_CLASSES` map).

**Why no `[sign]` table in `settings.toml`?** The device's `settings.toml` is intentionally small (Wi-Fi is managed by the Pi OS, MQTT is a flat table, panel geometry is a flat block). Adding a `[sign]` table means another surface for an operator to break (a typo in `effects = ["Flame",]` would either crash the boot or, worse, silently drop effects). The hard-coded defaults in code are unit-tested (the `tests/config_migrations_test.py` and `tests/effects_settings_test.py` suites assert them) and reviewed alongside the effect class definitions.

**Why was the issue's wording "the values in settings.toml just defaults"?** Re-reading the issue, the user said "Settings should be added to SignConfig, persisted to S3 and pushed to MQTT. This makes the values in settings.toml just defaults. They should be overridden as they get changed in config messages." — the *idea* is "settings.toml is no longer the source of truth; the runtime config is." The implementation choices are (a) keep the existing `settings.toml` as-is (no `effects` / `pacing` / `scroller` keys; nothing to override) and have the code defaults be the boot defaults, OR (b) add a `[sign]` table to `settings.toml` so the operator can override the code defaults in a flat file. Per the operator's later review, (a) is the right call: the existing `settings.toml` is small, the defaults are well-tested, and adding a `[sign]` table invites a second class of "where do I change this?" documentation. The runtime config (Flask UI → MQTT → device) is the single source of truth for these values.

The device's `settings.toml.example` is unchanged.

### Decision 4: `EffectsCoordinator` reads from `EffectsSettings`; `request_message` is removed; the `MessageManager` is the single recent-source

The coordinator's signature becomes:

```python
def __init__(
    self,
    display,
    scroller,
    effects,
    heart,
    message_manager,                      # NEW: single source of recent messages
    effect_settings,                       # NEW: EffectsSettings (pacing + recent_count + future)
    recent_count=5,                        # kept as an override for tests
):
```

The coordinator takes `EffectsSettings`, NOT the full `SignConfig`. It shouldn't know about filters, senders, sign name, or timezone — those are sign-state concerns, not coordinator concerns. If a future change adds a fourth "effects-related" field, it lands in `EffectsSettings`; if a future change adds a fifth "non-effects" field (say, a `theme` block), the coordinator still doesn't see it.

The `recent_provider` callable is gone. The coordinator reads recent bodies via `message_manager.get_messages(limit=self.recent_count, suppress=True)` (already the right shape — `MessageManager.get_messages` returns `MessageView`s, and `_random_recent` already extracts `.message.body`). The Pi passes `_message_mgr`; the browser preview passes its own `MessageManager` (which it already has, and which is already fed by the polling loop in `preview.js`). One path.

`request_message(text)` is removed. The coordinator's `_recent` deque was only used by the browser path (which is now served by the manager). New messages go through `MessageManager._handle_message` (which already appends to `InMemoryMessages`); the coordinator's `tick()` reads from the manager's ring buffer on the next idle-random-pick. The Pi's `on_message=lambda msg: coordinator.request_message(msg.body)` in `main.py:67` is removed; the new wiring is:

```python
_message_mgr = MessageManager(...)
asyncio.run(_message_mgr.seed())
# No on_message callback — the coordinator reads from _message_mgr directly.

coordinator = EffectsCoordinator(
    display, scroller, effects_list, heart=heartbeat,
    message_manager=_message_mgr, effect_settings=cfg.effect_settings,
)
```

The `EffectsSettings` fields are read in the constructor and stored as `self.fade_seconds`, `self.hold_seconds`, `self.intro_seconds`, `self.idle_seconds`, and `self.recent_count` — the coordinator's existing `tick()` doesn't need to change. If a config message arrives later, **the coordinator's pacing params don't update mid-run** (the fade-in-progress is using the old values, and the next mode transition uses the new ones — a clean handoff).

**Why doesn't the coordinator subscribe to config updates?** It would be cleaner, but it would also mean yet another subscriber on the MQTT feed and a thread-safety concern in the middle of `tick()`. The Pi re-reads the config on every `type="config"` envelope and re-constructs the coordinator + scroller + effect list. This is a heavier handoff than mid-run updates, but it's bounded, the fade-in-progress always completes cleanly, and the cost is one config message per UI save. (A future change can add live pacing updates if it matters; the issue doesn't ask for it.)

### Decision 5: Effect list is built from `effect_settings.effects` order (filtered by `enabled`), with graceful skip

`heart-matrix-controller/main.py` gains a small helper:

```python
_EFFECT_CLASSES = {
    "Hyperspace": Hyperspace,
    "VideoDisplay": VideoDisplay,
    "PngDisplay": PngDisplay,
    "Honeycomb": Honeycomb,
    "Flame": Flame,
    "Fireworks": Fireworks,
    "NightSky": NightSky,
    "Heartbeat": Heartbeat,  # boot-splash only; never in the rotation
}

def _build_effects(effect_settings, display):
    effects = []
    for entry in effect_settings.effects:
        if not entry.get("enabled", True):
            continue
        name = entry["name"]
        cls = _EFFECT_CLASSES.get(name)
        if cls is None:
            log.warning("Unknown effect in config: %r; skipping", name)
            continue
        try:
            effects.append(cls(display))
        except Exception:
            log.exception("Effect %r failed to initialize; skipping", name)
    return effects
```

The Pi calls `_build_effects(cfg.effect_settings, display)` instead of the hard-coded list. The helper iterates `effect_settings.effects` in order, skips entries with `enabled: false`, and skips entries whose `name` isn't in `_EFFECT_CLASSES` or whose constructor raises (logged at WARNING). `Heartbeat` is excluded from the rotation by being absent from `_DEFAULT_EFFECTS_LIST_FULL` (and is constructed separately for `coordinator.heart`). The same helper is reused on every config update, so a UI change to toggle `VideoDisplay` on takes effect on the next config message.

**Why iterate the full list (not just the enabled subset)?** Re-enabling an effect via the UI keeps its position in the list — the operator's "where does it go" decision is preserved. If the device only saw the enabled subset, the rotation order would be the operator's toggle order, not their original position decision. (A future weighted-random feature could collapse to just the enabled names; the order-aware path is what the v1 UI needs.)

**Why graceful skip?** The Pi boots even if a video file is missing (the current `VideoDisplay.__init__` will raise). The skip-and-log behavior is the same shape the preview's renderer already uses; a missing effect is not a fatal config error.

**Effect classes that need asset paths:** `PngDisplay` reads from `design/pngs/`, `VideoDisplay` from `design/videos/`. The Pi passes `display` to the constructor; the constructor is responsible for its own asset discovery. No change to those classes.

**Behavior change vs. the current device:** the current `main.py` includes `PngDisplay` and `VideoDisplay` in the rotation by default. With this change, the default rotation excludes them (they're `enabled: false` by default). An operator who wants them back toggles them on via the admin UI; a config without them just doesn't run them. The default is `enabled: false` for the asset-dependent effects because the asset files aren't always present on the device — running the rotation with the asset files missing would just log a skip on every cycle advance. The full 7-effect set is still visible in the UI, so the operator can see what they're choosing to enable.

### Decision 6: The settings page surfaces the new fields as one big "Effects" section + a "Text" section (Rendering Defaults removed)

`heart-message-manager/templates/settings.html` is a single Tailwind form that POSTs to `/settings` (Flask then PUTs to `/api/config`). The new sections:

- **Effects** (one big section, with two sub-sections) — the whole "what does the sign do?" panel:
  - **Effects List** sub-section — a checkbox per entry in `cfg.effect_settings.effects` (in the order they appear in the list), excluding `Heartbeat` (which is the boot-splash and is never in the rotation). Checked = `enabled: true`, unchecked = `enabled: false`; the form preserves the list order on save (the order is the rotation order). The checkboxes are populated from `cfg.effect_settings.effects` (returned by `GET /api/config`); no separate endpoint is needed. If a future effect is added to the device's `_EFFECT_CLASSES` map, the operator adds it to the list via the existing form (or via a config message); the UI doesn't auto-discover.
  - **Settings** sub-section — five labeled controls:
    - **Fade speed** (seconds for one full fade): range 0.1–10.0, step 0.1, default 2.0.
    - **Hold time** (seconds to keep a message fully visible): range 1–120, step 1, default 15.
    - **Intro time** (seconds for the boot-splash heart): range 0–30, step 0.5, default 5.
    - **Idle time** (seconds of idleness before a random message plays): range 30–3600, step 30, default 300.
    - **Recent messages** (size of the idle-rotation pool; wire name `recent_count`): range 1–20, step 1, default 5. Surfaced per operator feedback — they want to control "how many recent messages the idle rotation can pull from" without redeploying.

    Each control is a `<input type="range">` with a number input next to it (linked by JS, like the dashboard's existing slider/input pairs). The label is the human-readable name, not the technical name. The "fade speed" / "hold time" labels are what an operator thinks about; `fade_seconds` and `hold_seconds` are the wire names.

- **Text** — a single **Scroll speed** slider (mapped to `frame_delay`, inverse: 0 = slow = high `frame_delay`, 100 = fast = low `frame_delay`; the wire stores `frame_delay` in seconds per pixel, so the UI does the inverse mapping), a **Text color** color input (the existing hex color field, moved here from the old "Rendering Defaults" section which is being removed), and a **Text effect** dropdown showing only "scroll" in v1 (the dropdown is rendered but disabled with a tooltip "More text effects coming soon"). The `text_effect` is sent in the config anyway, so a future change that adds a real option lights up without a template change.

**Why "Effects List" + "Settings" inside Effects?** The previous draft had "Rotation" + "Behavior" — the operator pointed out that "Behavior" was too narrow (future fields added to the block won't all be behavior-related) and that "Pacing" was even worse (and confusing alongside a future "scroll pacing" block). "Settings" is the umbrella term for "configurable knobs inside this block" and is the most generic name that doesn't lock us in. The list sub-section is called "Effects List" (not just "Rotation") because "Rotation" implies a circular cycling, which is the current behavior but isn't the only possible one (a random walk or weighted random are future options).

**Why "Text" and not "Scrolling"?** The block is for the on-screen text rendering, not specifically for the scroller. The scroller is one text effect (the only one implemented in v1); future text effects (swirl, bounce) will share the same block. Naming the section "Text" leaves room for those; the dropdown showing only "scroll" today is the v1 limitation, not a long-term commitment to the name "Scrolling."

**Why is the old "Rendering Defaults" section removed?** The old section was a UI surface for the v1 `SignConfig.rendering` block (`speed`, `color`, `mode`). The new `text_settings` block replaces it: the text-color field moves to the new Text section, and the other v1 fields (`speed`, `mode`) are dropped because the device never actually consumed them (it used its own scroller constructor defaults). Keeping a "Rendering Defaults" section with just the text color would be a thin wrapper around a single field that's already in the Text section; the cleaner result is to remove the section entirely and let Text be the home for text rendering settings.

**Why is `recent_count` surfaced when the issue said "the goal isn't fine-tuning"?** The issue's guidance was "don't surface technical parameters operators don't need to reason about." `recent_count` is a knob operators DO want — it controls the variety of the idle rotation, which is a visible experience knob (a pool of 2 = the same two messages forever; a pool of 20 = wide variety). The other two `EffectsCoordinator` params that aren't surfaced (`fade_step` for throttle, `gamma` for perceptual brightness) stay as code defaults — those are the ones that "don't make sense unless you understand things like the refresh rate of the panel."

The form is still a single POST. The Flask handler reads the new fields from `request.form`, normalizes them, and PUTs the whole config.

**Why are sliders the only control?** The issue says "the goal isn't fine-tuning." Sliders express intent ("I want it to feel slower") better than number inputs ("fade = 2.3 seconds"). The number input next to the slider is a fallback for keyboard input and a readout of the current value, not the primary control.

### Decision 7: No separate `GET /api/effects` endpoint — the data lives in `GET /api/config`

The original draft of this change added a `GET /api/effects` endpoint to return the canonical effect set separately from the config. Reconsidered: the admin UI's only consumer of that list is the settings page's Effects List checkboxes, and the same data is already in `cfg.effect_settings.effects` (returned by `GET /api/config`). A separate endpoint would duplicate state and add a second round-trip on settings-page load. The UI now reads the list from `cfg.effect_settings.effects` directly.

The `lib_shared.models._DEFAULT_EFFECTS_LIST_FULL` constant IS the canonical effect set (in the `lib_shared/models.py` form, the full 7-entry list with `enabled` flags). The Flask process uses it for validation (rejecting unknown effect names in the PUT handler) and as the default for fresh installs. The device has its own `_EFFECT_CLASSES` map (the class-name → class binding) which is the same set; the unit tests assert the two are in sync.

**What about a future effect added to the device?** When a new effect class is added to `heart-matrix-controller/main.py`'s `_EFFECT_CLASSES` map, the operator adds it to the list via the existing settings form (or a config message) — there's no auto-discovery. The form's HTML is generated server-side from `cfg.effect_settings.effects`, so a new effect appears in the UI only when the config has it. This is intentional: the device is the source of truth for "what exists," and the config is the source of truth for "what's enabled and in what order"; the admin UI is a thin editor over the config.

**Why no `boot_splash` field either?** The `Heartbeat` boot-splash effect is the device's concern (constructed in `main.py`, passed as `coordinator.heart`); it never appears in the rotation. The UI doesn't need a separate field for it — it's not configurable in v1.

### Decision 8: Timezone offset is computed at read-time, not stored

`lib_shared/messages._format_display_time` no longer takes a `tz_offset_mins` parameter. It takes an IANA `timezone` string and computes the offset via `zoneinfo.ZoneInfo`:

```python
def _format_display_time(received_at: str, timezone: str) -> str:
    try:
        tz = ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError):
        tz = ZoneInfo("US/Pacific")
    utc_dt = datetime.fromisoformat(received_at.replace("Z", "+00:00"))
    local_dt = utc_dt.astimezone(tz)
    return local_dt.strftime("%Y-%m-%d %I:%M %p %Z").lower()
```

The offset is correct for the current instant (DST-aware). Storing it in the config was redundant.

**`server_time.tz_offset_mins(tz_name)` is removed.** The only caller is the SQLite recompute, which is also removed.

**The browser preview's offset path** was already pulling from the config; the new code path uses `ZoneInfo` in the browser too (the `is_browser` flag in `MessageManager` doesn't need to change).

### Decision 9: The dashboard status block surfaces the new fields

`heart-message-manager/static/app.js` reads `window.APP_CONFIG.effect_settings`, `effect_settings.effects`, and `text_settings` (NOT the old `rendering` block — it was removed) and renders them in a small "Sign settings" card. The card shows:

- Current effect rotation (comma-separated, the first one bolded as "next")
- Fade / hold / intro / idle values (human-readable labels)
- `recent_count` ("Recent messages: N")
- Scroll speed (in "pixels per second" for human consumption, computed as `1 / frame_delay`)
- Text effect (with the future-options tooltip)

This is informational only; the settings are still edited on `/settings`. The card exists so an operator glancing at the dashboard can confirm "yes, the config I saved is the config the device is using" without having to navigate.

### Decision 10: Wi-Fi credentials are out of scope for v1

The Pi OS owns Wi-Fi via `nmcli`. A bad write to the connection profile would disconnect the device, and the device is headless — there's no easy way to recover. The investigation is parked for a follow-up change that:

1. Adds a `[wifi]` table to `SignConfig` with `ssid` and `password` fields.
2. On receipt of a new wifi config, the Pi runs `nmcli device wifi connect "$SSID" password "$PASSWORD"` in a subprocess with a 30 s timeout. If the connection succeeds (exit 0), it persists via `nmcli connection modify` and reboots (or just `nmcli connection up`). If the connection fails, the new credentials are **discarded** and the device stays on the current connection.
3. A `[wifi]` section in the admin UI collects the new credentials, with a "Test before save" button that runs the subprocess on the Pi and reports the result.

This is a real chunk of work (subprocess + timeout + reboot + UI button + tests) and the issue's investigation tone ("I'm not sure if there's a way to test the new values before changing...?") suggests the operator wants to think about it more before committing. The design captures the constraint and the follow-up shape; v1 doesn't ship it.

### Decision 11: Config migrations via a small `MIGRATIONS` registry

The config shape is changing in a way that's a breaking read for any v1 consumer. Rather than write a custom parser that branches on `version` (and doubles the type surface), a `MIGRATIONS` registry brings older payloads up to the current version on every read. This is the smallest pattern that scales to v3+ without changing the read path; it's the same shape Rails' data migrations, Django's `migrations/`, and Pydantic v2's `model_validator(mode="before")` use, but minimal — for one config blob, an ORM migration framework is overkill.

**Module structure** (`lib_shared/config_migrations.py`):

```python
"""Config migrations: bring older SignConfig payloads up to the current version.

Each entry in MIGRATIONS is a `(dict) -> dict` callable that takes a payload at
version N and returns a payload at version N+1. Migrations run in order from
the payload's version up to the current version. Adding a new migration is a
3-step change: write the function, register it in MIGRATIONS, and bump
SignConfig.CURRENT_VERSION.
"""

from typing import Callable, Dict

from lib_shared.models import (
    _DEFAULT_EFFECTS_LIST_FULL,
    TextSettings,
)


def _v1_to_v2(d: dict) -> dict:
    """v1 → v2: drop tz_offset_mins + rendering, add effect_settings + text_settings, bump version.

    Preserves filters, senders, sign, timezone, version. The
    on-disk message list (messages.json in S3) is not part of the config and
    is not touched.
    """
    out = dict(d)
    out.pop("tz_offset_mins", None)
    out.pop("rendering", None)  # the old RenderingSettings block is being removed
    out.setdefault("effect_settings", {
        "effects": list(_DEFAULT_EFFECTS_LIST_FULL),  # full 7-entry list, 5 enabled + 2 disabled
        "fade_seconds": 2.0,
        "hold_seconds": 15.0,
        "intro_seconds": 5.0,
        "idle_seconds": 300.0,
        "recent_count": 5,
    })
    out.setdefault("text_settings", TextSettings().to_dict())
    out["version"] = 2
    return out


MIGRATIONS: Dict[int, Callable[[dict], dict]] = {
    1: _v1_to_v2,
}


def migrate(d: dict, current_version: int) -> dict:
    """Run all migrations needed to bring `d` up to `current_version`.

    If `d` has no version key, it's treated as v1. Each migration receives
    the output of the previous one (chained). Stops at `current_version`.
    Raises KeyError if a migration is missing for a required step.
    """
    version = int(d.get("version", 1))
    out = d
    for v in range(version, current_version):
        if v not in MIGRATIONS:
            raise KeyError(f"No migration registered for v{v} → v{v + 1}")
        out = MIGRATIONS[v](out)
    return out
```

**Call sites:**

- `SignConfig.from_dict(data)` — `data = migrate(data, current_version=cls.CURRENT_VERSION)` at the top, then construct the `SignConfig` from the migrated data.
- `SignConfig.update_from_dict(data)` — same migration call at the top, then the field-by-field update runs on the migrated data. This means `MessageManager._handle_config` gets migrations for free (it calls `update_from_dict`).
- Flask `PUT /api/config` handler — also runs `migrate(...)` on the incoming JSON before constructing the `SignConfig`. Defense in depth: the migration would happen inside `from_dict` anyway, but running it explicitly means the saved SQLite row is always at `CURRENT_VERSION` even if `from_dict` is later refactored.

**Idempotency:** `migrate(v2_payload, current_version=2)` returns `v2_payload` unchanged (the for-loop's `range(2, 2)` is empty). Tested explicitly.

**Why a registry and not a versioned file format or `Union[ConfigV1, ConfigV2]`?** The shape is small (one struct, ~8 fields), the migration list is short (one entry: v1 → v2), and the project doesn't have a database schema — the config is a single JSON blob. A registry that takes the current version and chains upgrade functions is the simplest mechanism that scales to v3+ without changing the read path. The alternative (parse by version, dispatch to a `ConfigV1` or `ConfigV2` class) doubles the type surface and doesn't help when v3 lands.

**Why a `version` flag and not just "drop unknown fields"?** Dropping unknown fields silently loses data — a v1 client receiving a v2 payload would lose `effect_settings`. The `version` flag makes the wire shape explicit: each consumer (Flask, device, browser preview) knows the shape it expects, and the migration registry brings older shapes forward. This is the same pattern that lets a v1 device and a v2 device coexist in the field during a rolling upgrade — the v2 device's `migrate(v1_payload)` returns a v2 payload; the v1 device simply never sees a v2 payload (because the Flask side normalized it on write).

**Why not Pydantic / Django migrations / Alembic?** All three are heavier than this project needs. Pydantic adds a dependency for what is currently plain Python objects; Django migrations assumes a database; Alembic assumes SQLAlchemy. The pattern in `MIGRATIONS` is the *idea* those frameworks implement — a chain of forward-only transforms keyed by version — but it doesn't need their machinery.

### Decision 12: Server runs the migration on startup (proactive, not lazy)

The migration registry from Decision 11 is necessary but not sufficient. A lazy "migrate on read" approach means that between the deploy of the new code and the first time an operator clicks "Save" on the settings page, the stored S3 config is still v1. The running Flask code reads it, runs the migration in memory, and serves a v2 response — but the S3 row and the SQLite row are still v1. If the operator never touches the settings page (the common case — the device is just sitting there displaying messages), the stored config stays at v1 indefinitely. The code that reads it has to be backward-compatible with v1 forever, and a future "drop v1 support" change is a breaking operation.

The fix is to run the migration on startup, in the existing "rebuild-from-S3 on startup" path:

1. After the existing step that reads the latest config from S3, run `migrate(...)` on the dict.
2. If a migration ran (i.e. the stored version is older than `SignConfig.CURRENT_VERSION`):
   - Write a new S3 entry at the current version, replacing the old one.
   - Update the local SQLite cache to the migrated config.
   - Publish a `type="config"` envelope to MQTT so any connected devices (Pi, browser preview) pick up the migrated config on their next MQTT read.
3. If the stored version is already at `CURRENT_VERSION`, the migration is a no-op and nothing is written or published.

The new function lives in `lib_shared/config_migrations.py` as `migrate_on_startup(s3_getter, sqlite_writer, mqtt_publisher)`, and is called from `heart-message-manager/main.py` (or `sqlite.py` — wherever the existing "rebuild-from-S3 on startup" lives) before the MQTT subscribe loop starts.

**Idempotency:** if the migration is re-run on a config that's already at `CURRENT_VERSION`, `migrate()` returns the input unchanged and `migrate_on_startup` short-circuits — no S3 write, no SQLite write, no MQTT publish. Tested explicitly.

**Why is this worth the complexity?** Because without it, the migration only happens the first time the operator saves settings — which could be months after the deploy. The whole point of the migration registry is to make the breaking config change transparent, and "transparent" means "the operator doesn't have to do anything." The startup hook is the cheapest way to make the change actually transparent.

**Why not also do this on the device?** The Pi doesn't have an S3 connection. Its in-memory config is built from the MQTT feed and the code defaults. If the server has migrated, the MQTT feed is at `CURRENT_VERSION` and the device's `update_from_dict` is a no-op. If the server hasn't migrated (e.g. the device boots before the server has finished its startup migration), the device uses the code defaults and applies the next MQTT config message when it arrives. The defense-in-depth migration in `update_from_dict` handles this case too.

**Why does the Flask PUT handler still run the migration explicitly?** Defense in depth: the migration would happen inside `from_dict` anyway, but running it explicitly means the saved SQLite row is always at `CURRENT_VERSION` even if `from_dict` is later refactored. The PUT handler is also the case where a v1 client (e.g. an older admin UI bookmark) could send a v1 payload; the explicit migration normalizes it.

## Risks / Trade-offs

- **[Risk] Coordinator + scroller don't update mid-run on a config message** → Mitigation: documented in Decision 4. The fade-in-progress completes with the old values, the next mode transition uses the new ones. The cost of a mid-run update is a thread-safety concern in `tick()`; the cost of a clean handoff is one fade-in-progress feeling slightly different from what the UI shows. Acceptable for v1.

- **[Risk] The v1 → v2 migration silently drops `tz_offset_mins`** → Mitigation: `tz_offset_mins` is a CircuitPython-era field whose value is now recomputed at read-time from the IANA `timezone`. The dropped value is recoverable from `timezone` (the same offset is produced by `ZoneInfo(timezone).utcoffset(<the stored received_at>)`). No information is lost; the field was a denormalization of `timezone`. Tested explicitly in `tests/config_migrations_test.py`.

- **[Risk] A v1 config in SQLite is read by the new code; the migration runs; but the stored row is still v1** → Mitigation: the **startup migration** in Decision 12 handles this — on the next server restart, the v1 S3 config is migrated and the new S3 entry + SQLite row are written at v2. Between deploy and the first restart, the in-memory config is v2 (the migration runs in `from_dict`); the wire shape is always v2 (Flask normalizes on read). After the first restart, the stored config is v2 and the in-memory config is v2. Tested explicitly.

- **[Risk] The hard-coded effect list in the device's `_EFFECT_CLASSES` can drift from the Flask `_DEFAULT_EFFECTS_LIST_FULL`** → Mitigation: a unit test in `tests/effects_settings_test.py` asserts the names in the device's `_EFFECT_CLASSES` map and the Flask `_DEFAULT_EFFECTS_LIST_FULL` are equal. If a future effect is added, the test fails until both are updated.

- **[Risk] The `text_effect` field is in the wire shape but is ignored by the scroller** → Mitigation: the dropdown is rendered disabled with a tooltip. The field is stored and round-tripped. The next change (a swirl or bounce effect) is a one-line addition to the scroller's `__init__`.

- **[Risk] The settings page POST has more fields now → bigger form payload, more validation surface** → Mitigation: validation is centralized in `_build_sign_config_from_request`, which returns `(SignConfig, error_response_or_None)`. Per-field error messages are returned as JSON when the request is `Content-Type: application/json` and as a flashed-message + redirect when it's a form POST.

- **[Trade-off] The full 7-effect set is stored with `enabled: false` for the asset-dependent defaults** → Accepted. Storing the full set means the operator's "which effects exist" and "which are on" answers live in one place (the `effects` list). Re-enabling `VideoDisplay` later just flips its `enabled` flag; the device picks it up on the next config message. The list IS the rotation when filtered by `enabled: true` — no separate "enabled set" data structure. The trade-off is one extra field per entry (`enabled`), but the UI gets a stable shape and re-enabling is a one-click operation.

- **[Trade-off] `tz_offset_mins` AND `rendering` removal are breaking wire changes for any consumer that still reads them** → Accepted. The only consumers are inside this repo (`lib_shared/messages.py`, `heart-message-manager/sqlite.py`, `static/app.js`, `templates/settings.html`, `tests/test_message_manager.py`, `tests/test_auth.py`); all are updated in this change. The external Adafruit IO feed has no other consumer.

- **[Trade-off] Default effect rotation excludes `PngDisplay` and `VideoDisplay` (behavior change vs. the current device)** → Accepted. The current device's `main.py` constructs 7 effects including the two asset-dependent ones; the new default rotation is 5 effects (the same 5 historically-defaulted effects, with `VideoDisplay` and `PngDisplay` present but `enabled: false`). An operator who wants the full 7-effect rotation flips their `enabled` flags via the admin UI once they have the asset files in place. The default has them disabled because the asset files aren't always present on the device — running the rotation with the asset files missing would just log a skip on every cycle advance, which is worse than not including them by default. The skip-and-log path still exists for the disabled-but-flipped-on case (the device tries to construct, fails, warns, continues), so the device is robust either way.

## Migration Plan

This is a wire-breaking change to `SignConfig`, so there IS a migration step (the v1 → v2 payload transform). The migration runs in two places: the read path (transparent to operators on every read) AND the server's startup hook (proactive — writes the migrated config to S3, SQLite, and MQTT so the running code only ever sees the current version).

**Config payload migration (transparent on read, proactive on startup):**

- Existing `version: 1` configs in S3 and SQLite are migrated to v2 on every read by the `MIGRATIONS` registry. Filters, senders, sign name, and timezone are all preserved. The on-disk message list (`messages.json` in S3) is not part of the config and is not touched. The `tz_offset_mins` field is dropped (its value is recoverable from `timezone` at read-time). The old `rendering` block is also dropped — it's being removed entirely and replaced by the new `text_settings` block. The new `effect_settings` and `text_settings` blocks are added with their code defaults.
- **On server startup** (see Decision 12), the server reads the latest config from S3, runs `migrate(...)`, and if a migration ran, writes a new S3 entry, updates the local SQLite cache, and publishes a `type="config"` envelope to MQTT. This is the central design decision: the running code only ever sees `CURRENT_VERSION` — it doesn't have to be backward-compatible with old shapes for months waiting for an operator to click "Save."
- The Flask `GET /api/config` endpoint returns the migrated config (Flask normalizes on read; the migration would happen inside `from_dict` if the row is then reloaded, but the GET path runs it explicitly so the response is always at the current version).
- The Flask `PUT /api/config` endpoint runs the migration on the incoming payload before constructing the `SignConfig`. The saved SQLite row is always at `CURRENT_VERSION`.
- The device's `MessageManager._handle_config` calls `update_from_dict`, which runs the migration at the top. A v1 payload arriving over MQTT (e.g. from a stale MQTT message cached before the server's startup migration) is upgraded to v2 in the device's in-memory config.

**Deployment steps:**

1. Add `lib_shared/config_migrations.py` (with `MIGRATIONS`, `migrate()`, `_v1_to_v2`, and `migrate_on_startup()`). Add `EffectsSettings`, `TextSettings`, and `_DEFAULT_EFFECTS_LIST_FULL` to `lib_shared/models.py` (no separate files). Delete the old `RenderingSettings` class. Add the two new top-level fields to `SignConfig`; remove `tz_offset_mins` AND `rendering`. Bump `version` default to 2; add `CURRENT_VERSION = 2` class constant. Wire `migrate(...)` into `from_dict` and `update_from_dict`.
2. Add a startup hook in `heart-message-manager/main.py` (or `sqlite.py` — wherever the existing "rebuild-from-S3 on startup" lives) that calls `migrate_on_startup(...)` after the S3 read.
3. Refactor `EffectsCoordinator` to accept `effect_settings: EffectsSettings`; replace `request_message` and `recent_provider` with a single `message_manager` argument.
4. Update `heart-matrix-controller/main.py` to build the effect list from `cfg.effect_settings.effects` and pass `cfg.effect_settings` + `_message_mgr` to the coordinator.
5. Update `heart-matrix-controller/scroller.py` to read from `TextSettings`.
6. Update `heart-message-manager/main.py`: extend `PUT /api/config` validation to the new fields (effects entries must have a `name` from the device's known set and a boolean `enabled`; behavior fields must be non-negative; recent_count must be a positive integer; text fields must be non-negative; text_effect must be one of an enum), add `_build_sign_config_from_request` helper, run `migrate(...)` on the incoming JSON. No new endpoint — the admin UI reads the effects list from `GET /api/config`.
7. Update `heart-message-manager/templates/settings.html` with the new one-big-Effects-section structure (Effects List + Settings sub-sections) and the Text section. The old "Rendering Defaults" section is removed.
8. Update `lib_shared/messages.py` to compute the offset at read-time via `zoneinfo`.
9. Update `heart-message-manager/sqlite.py` to remove the `tz_offset_mins` recompute.
10. Update `heart-message-manager/preview_main.py` to remove the `request_message` call (the coordinator now reads from the manager).
11. Update `heart-message-manager/static/app.js` to surface the new fields in the dashboard.
12. Update test fixtures that reference `tz_offset_mins`.

**Rollback:** revert the commit. The Flask endpoint accepts the old payload (it ignores unknown keys, and the new payload is a strict superset minus `tz_offset_mins` and `rendering`). The device reads the new fields with defaults, so a rollback is a no-op for a device that never received a new config message. The migration registry is forward-only; a rolled-back device reading a v2 config would lose the new fields' values (the old code doesn't know about `effect_settings` or `text_settings`). For a clean rollback, also revert any v2 config the operator has saved in the interim (or, equivalently, deploy the rollback to the device before the Flask side sees any v2 writes). The startup migration in Decision 12 is forward-only and writes new S3 entries; on a rollback, those entries are still at v2 and will be re-migrated on the next startup, so the rollback is safe to deploy and re-deploy.

## Open Questions

- **Wi-Fi credentials**: parked for a follow-up change. See Decision 10 for the proposed shape (subprocess + timeout + pre-commit test).
- **Live pacing updates**: the coordinator doesn't re-read its pacing params mid-run. If a future change wants "save in the UI, see the new fade speed within 1 s," the coordinator needs a `set_effect_settings(effect_settings)` method that takes the lock and updates the fields. The fade-in-progress uses the old values; the next mode transition uses the new ones. Out of scope for v1.
- **Effect ordering in the UI**: the issue says the UI should be able to toggle effects on/off; it doesn't say the order should be editable. The current design has the order fixed (the hard-coded `_DEFAULT_EFFECTS_LIST_FULL` order, which is the canonical order shared between Flask and the device). The full list IS the order; toggling an effect on/off doesn't reorder it. If the issue evolves to "let me reorder the rotation," the easiest path is drag-and-drop on the checkboxes, which writes the new order back as a list. Parked.
- **A separate "preset" / "theme" abstraction**: not asked for. The config is the config. If the operator wants "Christmas mode" later, that's an `effect_settings.effects` list + a `hold_seconds` value saved as a config — not a new abstraction.
- **Startup migration failure mode**: if the S3 read fails on startup (e.g. credentials rotated), the existing code path raises. The startup migration doesn't change that — it just adds a step after the read succeeds. If the S3 write-back fails (e.g. transient network blip), the server logs the error and continues; the next startup will re-attempt. The in-memory config is at v2 regardless. The biggest risk is a partial write where S3 is updated but the SQLite/MQTT updates fail; this is mitigated by the fact that the S3 update is the last step (so a failure leaves the in-memory + SQLite + MQTT at v2 even if S3 is still v1) — actually, the recommended order is SQLite first (local, fast), then MQTT, then S3 (eventual consistency), so a failure between MQTT and S3 leaves the device seeing v2, the server's SQLite at v2, and S3 at v1 (the next startup will re-migrate, idempotently). Acceptable for v1; a future change can add a "last successful migration" marker to make the failure case more visible.
