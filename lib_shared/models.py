"""Plain Python models shared between the Flask app and the Raspberry Pi display."""

import json
import logging
import threading
from typing import List, Optional

from lib_shared.phone_utils import normalize_phone

log = logging.getLogger("heart")


def _parse_senders_wire(entries: list) -> dict[str, dict]:
    """Convert the wire ``senders`` list into the internal dict-of-dict shape.

    Each wire entry is ``{"phone", "name", "action"?, "status"?}``. The
    returned dict is keyed by the NORMALIZED phone (via
    ``phone_utils.normalize_phone``) so lookups after normalizing an incoming
    sender are O(1). The value preserves the operator's original ``phone``
    string for round-trip display fidelity. Missing ``action`` defaults to
    ``"allow"``; missing ``status`` defaults to ``"enabled"`` (back-compat for
    partial / legacy payloads — the migration normally backfills both).
    """
    result: dict[str, dict] = {}
    for entry in entries:
        original_phone = entry["phone"]
        result[normalize_phone(original_phone)] = {
            "name": entry.get("name", ""),
            "action": entry.get("action", "allow"),
            "status": entry.get("status", "enabled"),
            "phone": original_phone,
        }
    return result


def _senders_to_wire(senders: dict[str, dict]) -> list[dict]:
    """Serialize the internal senders dict to the wire list shape.

    Emits each value's original ``phone`` (not the normalized key), sorted by
    phone for deterministic output.
    """
    return [
        {
            "phone": value["phone"],
            "name": value["name"],
            "action": value["action"],
            "status": value["status"],
        }
        for value in sorted(senders.values(), key=lambda v: v["phone"])
    ]


class MessageEnvelope:
    """JSON envelope for the unified MQTT feed.

    Attributes:
        type:    "message" | "config"
        payload: dict — Message.to_dict() or SignConfig.to_dict()
    """

    def __init__(self, type: str, payload: dict) -> None:
        """Initialize a MessageEnvelope.

        Args:
            type: Envelope type — "message" or "config".
            payload: Dict payload — Message.to_dict() or SignConfig.to_dict().
        """
        self.type = type
        self.payload = payload

    @classmethod
    def from_json(cls, raw: str) -> "MessageEnvelope":
        d = json.loads(raw)
        return cls(type=d["type"], payload=d["payload"])

    def to_json(self) -> str:
        return json.dumps({"type": self.type, "payload": self.payload}, separators=(",", ":"))


class Message:
    """Represents an incoming SMS message.

    Stored to S3 as JSON and published over MQTT as part of a MessageEnvelope.

    `media` carries the optional list of MMS attachments that landed alongside
    the text. Each entry is `{"type": str, "url": str}` — `type` is the MIME
    type (e.g. ``"image/jpeg"``) and `url` is the S3 key under our bucket
    (e.g. ``"media/images/2026-07/media-2026-07-09T15-30-00Z.jpg"``). SMS-only
    messages carry ``media == []``. S3 keys are the wire format
    (design D2) — never a Twilio MediaUrl, never a pre-signed URL. The 1-hour
    signed URL is regenerated on every Flask 302 (see
    ``GET /api/media/<path:key>``).
    """

    def __init__(
        self,
        id: str,
        sender: str,
        body: str,
        received_at: str,
        media: Optional[list[dict]] = None,
    ) -> None:
        """Initialize a Message.

        Args:
            id: Unique message identifier (UUID string).
            sender: Phone number of the sender.
            body: Text content of the message.
            received_at: ISO 8601 UTC timestamp when received.
            media: Optional list of ``{"type": str, "url": str}`` entries
                representing MMS attachments already copied to OUR S3.
                Defaults to an empty list (legacy 4-field wire shape
                round-trips unchanged).
        """
        self.id = id
        self.sender = sender
        self.body = body
        self.received_at = received_at
        # `media` MUST always round-trip through to_dict/from_dict with the
        # exact list the caller passes in — empty for SMS, populated for MMS.
        # Defensive copy via list(...) keeps mutating `d["media"]` after
        # construction from leaking into self.media.
        self.media: list[dict] = list(media) if media is not None else []

    @classmethod
    def from_dict(cls, d):
        return cls(
            id=d["id"],
            sender=d["sender"],
            body=d["body"],
            received_at=d["received_at"],
            media=d.get("media", []),
        )

    def to_dict(self):
        return {
            "id": self.id,
            "sender": self.sender,
            "body": self.body,
            "received_at": self.received_at,
            "media": self.media,
        }


class MessageView:
    """Message with source and computed suppression status."""

    def __init__(
        self,
        message: "Message",
        source: str = "rest",
        suppressed: bool = False,
        rules: list["FilterRule"] | None = None,
        sender_name: str = "",
        display_time: str | None = None,
        media: list | None = None,
    ) -> None:
        """Initialize a MessageView.

        Args:
            message: Message object this view wraps.
            source: "rest" when loaded from storage, "mqtt" when received live.
            suppressed: True if any filter rule matched this message.
            rules: List of FilterRule objects that suppressed the message.
            sender_name: Display name for the sender (from the senders allowlist).
            display_time: Pre-formatted local time string, or None (set by _enrich_messages).
            media: Optional MMS attachments list. Defaults to
                ``message.media`` if not supplied — surface it as a
                flat top-level attribute so the JS-side Pyodide
                proxy exposes ``entry.media`` alongside ``source`` /
                ``display_time``. Without this, the testing page's
                modal (which ``JSON.stringify``s the entry) sees
                ``media`` nested under ``entry.message.media`` and
                the inline row click / the modal popup disagree.
        """
        self.message = message
        self.source = source
        self.suppressed = suppressed
        self.rules = list(rules) if rules is not None else []
        self.sender_name = sender_name
        self.display_time = display_time
        # Mirror the wrapped Message's `media` so it's a flat field on
        # the view — JS-side Pyodide proxies only expose instance
        # attributes set in __init__, not @property accessors, so this
        # has to be a real attribute for `item.media` and
        # `JSON.stringify(item).media` to work on the testing page.
        self.media = list(media) if media is not None else list(message.media)

    def to_dict(self):
        return {
            "id": self.message.id,
            "sender": self.message.sender,
            "body": self.message.body,
            "received_at": self.message.received_at,
            "media": self.message.media,
            "source": self.source,
            "suppressed": self.suppressed,
            "rules": self.rules,
            "sender_name": self.sender_name,
            "display_time": self.display_time,
        }


class FilterRule:
    """A single filter rule that can suppress messages.

    Attributes:
        type: Rule type — one of ``"keyword"``, ``"regex"``, ``"message"``.
            The ``"sender"`` type was REMOVED from the wire in v3 — sender
            matching is now the sole responsibility of ``SignConfig.senders``.
        pattern: The value to match against (case-sensitive except for keyword).
        action: Always ``"suppress"`` in v1 (the only accepted value on the
            wire). ``"allow"`` is deferred to a future change.
        status: The LIFECYCLE axis — ``"enabled"`` (the rule is on) or
            ``"disabled"`` (muted without deleting). Default ``"enabled"``.
    """

    #: The set of rule types accepted on the wire in v3. ``"sender"`` is gone.
    VALID_TYPES: frozenset = frozenset({"keyword", "regex", "message"})

    def __init__(
        self,
        type: str,
        pattern: str,
        action: str = "suppress",
        status: str = "enabled",
    ) -> None:
        """Initialize a FilterRule.

        Args:
            type: Rule type — one of ``"keyword"``, ``"regex"``, ``"message"``.
            pattern: Value to match against.
            action: Action to take when matched (default ``"suppress"``; the
                only accepted value in v1).
            status: Lifecycle flag — ``"enabled"`` (default) or ``"disabled"``.
        """
        self.type = type
        self.pattern = pattern
        self.action = action
        self.status = status

    @classmethod
    def from_dict(cls, d):
        """Parse a FilterRule from a wire dict.

        Raises:
            ValueError: if ``type`` is not one of ``keyword``/``regex``/
                ``message`` (notably rejecting the removed ``"sender"`` type),
                or if ``action`` is any value other than ``"suppress"``.
        """
        rule_type = d["type"]
        if rule_type not in cls.VALID_TYPES:
            raise ValueError(
                f"FilterRule.type must be one of {sorted(cls.VALID_TYPES)}, got {rule_type!r} "
                "(the 'sender' type was removed in v3 — use SignConfig.senders instead)"
            )
        action = d.get("action", "suppress")
        if action != "suppress":
            raise ValueError(f"FilterRule.action must be 'suppress' in v1, got {action!r}")
        return cls(
            type=rule_type,
            pattern=d["pattern"],
            action=action,
            status=d.get("status", "enabled"),
        )

    def to_dict(self):
        return {
            "type": self.type,
            "pattern": self.pattern,
            "action": self.action,
            "status": self.status,
        }


class SignSettings:
    """Sign configuration with name attribute."""

    def __init__(self, name: str = "Lindsay's Heart"):
        """Initialize SignSettings.

        Args:
            name: Display name shown on the sign (default "Lindsay's Heart").
        """
        self.name = name

    @classmethod
    def from_dict(cls, d):
        if d is None:
            return cls()
        return cls(name=d.get("name", "Lindsay's Heart"))

    def to_dict(self):
        return {"name": self.name}


# ---------------------------------------------------------------------------
# EffectsSettings + TextSettings — v2 config blocks
# ---------------------------------------------------------------------------


def _default_effects_list() -> List[dict]:
    """Return the canonical effects list (loaded via the loader).

    Reads `lib_shared.effects_loader.load_effects_settings()["effects"]`
    on first call and strips the loader-only `module` / `class_name`
    fields (the dataclass only needs `name` and `enabled`). Caches the
    result on the function attribute so subsequent calls are cheap.

    Imports are deferred to the function body so the module-level
    import of `models.py` doesn't pull in the loader at the wrong
    moment (the loader module imports `os`, `pathlib`, etc., and we
    want `models.py` to remain importable in any environment).
    """
    cached = getattr(_default_effects_list, "_cache", None)
    if cached is not None:
        return cached
    from lib_shared.effects_loader import load_effects_settings

    entries = load_effects_settings().get("effects", [])
    cleaned = [{"name": e["name"], "enabled": e["enabled"]} for e in entries]
    _default_effects_list._cache = cleaned  # type: ignore[attr-defined]
    return cleaned


class EffectsSettings:
    """Effects subsystem config: rotation list + pacing + recent_count.

    Groups every input the `EffectsCoordinator` consumes so the coordinator
    takes one focused argument instead of the full SignConfig.
    """

    def __init__(
        self,
        effects: Optional[List[dict]] = None,
        fade_seconds: Optional[float] = None,
        hold_seconds: Optional[float] = None,
        intro_seconds: Optional[float] = None,
        idle_seconds: Optional[float] = None,
        recent_count: Optional[int] = None,
    ):
        """Initialize EffectsSettings.

        Args:
            effects: List of `{"name": str, "enabled": bool}` dicts. Defaults
                to the canonical list loaded via `lib_shared.effects_loader`.
            fade_seconds: Seconds for one full fade. Default `None` falls
                through to the loader's value (canonical or operator
                override) so an `effects_settings` override honors its
                own pacing.
            hold_seconds: Seconds to keep a message fully visible. Default
                `None` falls through to the loader.
            intro_seconds: Seconds to show the boot-splash heart. Default
                `None` falls through to the loader.
            idle_seconds: Seconds of idleness before a random message
                plays. Default `None` falls through to the loader.
            recent_count: Size of the idle-rotation recent-messages pool.
                Default `None` falls through to the loader.

        The loader-driven defaults are what make an operator's override
        file (env var or `config_overrides/effects_settings.json`) take
        effect even when the device boots without any wire envelope —
        the device constructs `EffectsSettings()` directly and the
        pacing values come from the same source the loader writes.
        """
        # Read the loader output once so the override (env var or
        # config_overrides/) and canonical fall through identically for
        # the effects list and every pacing field. The same source-of-
        # truth path that `_default_effects_list` already uses.
        from lib_shared.effects_loader import load_effects_settings

        loader_cfg = load_effects_settings()

        self.effects = list(effects) if effects is not None else [dict(e) for e in _default_effects_list()]
        self.fade_seconds = fade_seconds if fade_seconds is not None else loader_cfg.get("fade_seconds", 2.0)
        self.hold_seconds = hold_seconds if hold_seconds is not None else loader_cfg.get("hold_seconds", 15.0)
        self.intro_seconds = intro_seconds if intro_seconds is not None else loader_cfg.get("intro_seconds", 5.0)
        self.idle_seconds = idle_seconds if idle_seconds is not None else loader_cfg.get("idle_seconds", 300.0)
        self.recent_count = recent_count if recent_count is not None else loader_cfg.get("recent_count", 5)

    @classmethod
    def from_dict(cls, d):
        """Parse from a dict (wire shape).

        Args:
            d: dict with optional keys: effects, fade_seconds, hold_seconds,
                intro_seconds, idle_seconds, recent_count.

        Returns:
            A new EffectsSettings instance.

        Raises:
            ValueError: on a malformed effects list (entries missing name/enabled,
                or non-dict entries). The Flask validation layer is responsible
                for rejecting unknown effect names; this method accepts any
                name string.
        """
        d = d or {}
        effects = d.get("effects", _default_effects_list())
        if not isinstance(effects, list) or not all(
            isinstance(n, dict) and isinstance(n.get("name"), str) and isinstance(n.get("enabled"), bool)
            for n in effects
        ):
            raise ValueError("effects must be a list of {name: str, enabled: bool} objects")

        # Pacing fields are passed as `None` when absent so `__init__`
        # can fall through to the loader (canonical or operator
        # override). This way an empty wire envelope (`{}`) on a device
        # that boots with no config sync still gets the operator's
        # pacing values from the override file. When the field IS
        # present, it gets type-coerced here (str→float, str→int) so
        # the wire layer can tolerate JSON strings from older clients
        # while the explicit-value path in `__init__` gets a typed
        # value.
        def _f(key: str) -> Optional[float]:
            v = d.get(key)
            return None if v is None else float(v)

        def _i(key: str) -> Optional[int]:
            v = d.get(key)
            return None if v is None else int(v)

        return cls(
            effects=[{"name": n["name"], "enabled": n["enabled"]} for n in effects],
            fade_seconds=_f("fade_seconds"),
            hold_seconds=_f("hold_seconds"),
            intro_seconds=_f("intro_seconds"),
            idle_seconds=_f("idle_seconds"),
            recent_count=_i("recent_count"),
        )

    def to_dict(self):
        """Serialize to a dict (wire shape)."""
        return {
            "effects": self.effects,
            "fade_seconds": self.fade_seconds,
            "hold_seconds": self.hold_seconds,
            "intro_seconds": self.intro_seconds,
            "idle_seconds": self.idle_seconds,
            "recent_count": self.recent_count,
        }

    def validate(self):
        """Raise ValueError on out-of-range values.

        Raises:
            ValueError: on negative pacing durations, recent_count < 1, or
                a malformed effects list.
        """
        if self.fade_seconds < 0 or self.hold_seconds < 0 or self.intro_seconds < 0 or self.idle_seconds < 0:
            raise ValueError("pacing durations must be non-negative")
        if self.recent_count < 1:
            raise ValueError("recent_count must be a positive integer")
        if not isinstance(self.effects, list) or not all(
            isinstance(n, dict) and isinstance(n.get("name"), str) and isinstance(n.get("enabled"), bool)
            for n in self.effects
        ):
            raise ValueError("effects must be a list of {name: str, enabled: bool} objects")


class TextSettings:
    """Text rendering config: scroll speed, color, text_effect.

    `speed` is the user-facing knob (1=Low to 5=High). The underlying
    `frame_delay` / `offset_seconds` are derived from it by the scroller
    (see `ScrollerBase.SPEED_TABLE`). The wire shape stores `speed` only
    — the technical pacing values are device-local.

    Named "text_settings" (not "scroller_settings") because the scroller
    is just one text effect — future text effects (swirl, bounce) will
    share the same block.
    """

    # v1 supports "scroll" only; more values land as future text effects.
    TEXT_EFFECTS: tuple = ("scroll",)
    MIN_SPEED = 1
    MAX_SPEED = 5
    DEFAULT_SPEED = 3

    def __init__(
        self,
        speed: int = DEFAULT_SPEED,
        color: int = 0xFF0000,
        text_effect: str = "scroll",
    ):
        """Initialize TextSettings.

        Args:
            speed: 1..5 scroll speed (1=Low, 3=Medium default, 5=High).
            color: 24-bit RGB color value (default 0xFF0000 red).
            text_effect: One of TEXT_EFFECTS (currently "scroll").
        """
        self.speed = speed
        self.color = color
        self.text_effect = text_effect

    @classmethod
    def from_dict(cls, d):
        """Parse from a dict (wire shape).

        Args:
            d: dict with optional keys: speed, color, text_effect. Legacy
                `frame_delay` / `offset_seconds` keys are silently ignored —
                the new defaults are sensible and the user said v2 payloads
                are disposable.

        Returns:
            A new TextSettings instance.

        Raises:
            ValueError: on an unknown text_effect, or on an out-of-range
                or non-integer `speed` (callers like the admin validation
                helper catch and translate to a 400).
        """
        d = d or {}
        text_effect = d.get("text_effect", "scroll")
        if text_effect not in cls.TEXT_EFFECTS:
            raise ValueError(f"text_effect must be one of {cls.TEXT_EFFECTS}, got {text_effect!r}")
        speed = d.get("speed", cls.DEFAULT_SPEED)
        if isinstance(speed, bool) or not isinstance(speed, int) or not cls.MIN_SPEED <= speed <= cls.MAX_SPEED:
            raise ValueError(f"speed must be an integer in {cls.MIN_SPEED}..{cls.MAX_SPEED}, got {speed!r}")
        return cls(
            speed=speed,
            color=int(d.get("color", 0xFF0000)),
            text_effect=text_effect,
        )

    def to_dict(self):
        """Serialize to a dict (wire shape)."""
        return {
            "speed": self.speed,
            "color": self.color,
            "text_effect": self.text_effect,
        }

    def validate(self):
        """Raise ValueError on out-of-range values.

        Raises:
            ValueError: on speed outside 1..5, color outside 0..0xFFFFFF,
                or an unknown text_effect.
        """
        if (
            isinstance(self.speed, bool)
            or not isinstance(self.speed, int)
            or not self.MIN_SPEED <= self.speed <= self.MAX_SPEED
        ):
            raise ValueError(f"speed must be an integer in {self.MIN_SPEED}..{self.MAX_SPEED}")
        if not (0 <= self.color <= 0xFFFFFF):
            raise ValueError("color must be in range 0..0xFFFFFF")
        if self.text_effect not in self.TEXT_EFFECTS:
            raise ValueError(f"text_effect must be one of {self.TEXT_EFFECTS}")


class SignConfig:
    """Configuration data model for the sign.

    filters: list of FilterRule objects
    senders: dict mapping NORMALIZED phone -> ``{"name", "action", "status",
        "phone"}`` value object (see the ``senders-status`` capability). The
        dict key is the normalized phone (``phone_utils.normalize_phone``);
        the value's ``phone`` field preserves the operator's original input.
    sign: SignSettings
    timezone: IANA timezone string
    effects_settings: EffectsSettings
    text_settings: TextSettings

    Thread-safe: guards mutations with a reentrant lock.
    """

    # Wire-format schema version. Bump on breaking changes; pair with
    # a new entry in lib_shared.config_migrations.MIGRATIONS.
    CURRENT_VERSION: int = 3

    def __init__(
        self,
        filters: list["FilterRule"] | None = None,
        senders: dict[str, dict] | None = None,
        sign: "SignSettings | dict | None" = None,
        timezone: str = "US/Pacific",
        version: int = CURRENT_VERSION,
        effects_settings: "EffectsSettings | dict | None" = None,
        text_settings: "TextSettings | dict | None" = None,
    ) -> None:
        """Initialize a SignConfig.

        Args:
            filters: List of FilterRule objects (default empty).
            senders: Dict mapping normalized phone -> ``{"name", "action",
                "status", "phone"}`` value object (default empty).
            sign: SignSettings instance or dict (default built from empty dict).
            timezone: IANA timezone string (default "US/Pacific").
            version: Config schema version (default CURRENT_VERSION = 3).
            effects_settings: EffectsSettings instance or dict (default built from empty dict).
            text_settings: TextSettings instance or dict (default built from empty dict).
        """
        self.filters = filters or []
        self.senders = senders or {}
        self.sign = sign if isinstance(sign, SignSettings) else SignSettings.from_dict(sign or {})
        self.timezone = timezone
        self.version = version
        self.effects_settings = (
            effects_settings
            if isinstance(effects_settings, EffectsSettings)
            else EffectsSettings.from_dict(effects_settings or {})
        )
        self.text_settings = (
            text_settings if isinstance(text_settings, TextSettings) else TextSettings.from_dict(text_settings or {})
        )
        self._lock = threading.RLock()

    def _with_lock(self, fn):
        """Run fn under the config lock (no-op if lock unavailable).

        Args:
            fn: a callable to execute inside the lock.

        Returns:
            The return value of fn().
        """
        if self._lock:
            with self._lock:
                return fn()
        return fn()

    @classmethod
    def default(cls):
        """Return a default SignConfig with empty filters, senders, and US/Pacific timezone."""
        return cls()

    @classmethod
    def from_dict(cls, data):
        """Deserialize a SignConfig from a dict (the same shape as to_dict()).

        Runs the migration registry at the top so older wire shapes are
        transparently brought up to CURRENT_VERSION.

        Args:
            data: dict with optional keys: filters, senders, sign, timezone,
                version, effects_settings, text_settings.

        Returns:
            A new SignConfig instance.
        """
        # Defense-in-depth: bring older payloads forward before parsing.
        from lib_shared.config_migrations import migrate

        if data is not None:
            data = migrate(data, current_version=cls.CURRENT_VERSION)
        else:
            data = {}
        return cls(
            filters=[FilterRule.from_dict(f) for f in data.get("filters", [])],
            senders=_parse_senders_wire(data.get("senders", [])),
            sign=(SignSettings.from_dict(data.get("sign")) if data.get("sign") else SignSettings()),
            timezone=data.get("timezone", "US/Pacific"),
            version=data.get("version", cls.CURRENT_VERSION),
            effects_settings=data.get("effects_settings"),
            text_settings=data.get("text_settings"),
        )

    def to_dict(self):
        """Serialize the config to a dict suitable for JSON or S3 storage.

        Returns:
            dict with keys: filters, senders, sign, timezone, effects_settings,
            text_settings, version.
        """
        return self._with_lock(
            lambda: {
                "filters": [f.to_dict() for f in self.filters],
                "senders": _senders_to_wire(self.senders),
                "sign": self.sign.to_dict(),
                "timezone": self.timezone,
                "version": self.version,
                "effects_settings": self.effects_settings.to_dict(),
                "text_settings": self.text_settings.to_dict(),
            }
        )

    def update(self, other: "SignConfig") -> None:
        """Replace all fields with values from another SignConfig (thread-safe).

        Subclasses can override this to persist to storage (e.g. SqliteConfig).
        """

        def _do():
            self.filters = other.filters
            self.senders = other.senders
            self.sign = other.sign
            self.timezone = other.timezone
            self.version = other.version
            self.effects_settings = other.effects_settings
            self.text_settings = other.text_settings

        self._with_lock(_do)

    def update_from_dict(self, data: dict) -> None:
        """Replace all fields from a dict (mutates self). Thread-safe.

        Runs the migration registry at the top so older wire shapes (a v1
        payload arriving over MQTT, for example) are transparently brought
        up to CURRENT_VERSION before the field-by-field update runs.

        If an `effects_settings` override is active on this process
        (`config_overrides/effects_settings.json` or
        `EFFECTS_SETTINGS_OVERRIDE` env var), the wire's `effects_settings`
        block is dropped here so EVERY entry point (seed fetch, MQTT
        dispatch, future callers) respects the override. The override owns
        the entire `EffectsSettings` — both the effects list AND the pacing
        fields. `MessageManager._handle_config` also strips defensively;
        that path stays (defense in depth, no-op when this layer has
        already stripped).
        """
        from lib_shared.config_migrations import migrate

        if data is not None:
            data = migrate(data, current_version=self.CURRENT_VERSION)
        else:
            data = {}

        # Strip wire `effects_settings` when an override is active. The
        # override lives in the loader (canonical repo-root JSON or
        # `EFFECTS_SETTINGS_OVERRIDE` env var) and is the single source of
        # truth for this block. We must not mutate the caller's dict, so
        # shallow-copy before pop.
        from lib_shared.effects_loader import is_effects_settings_override_active

        if is_effects_settings_override_active() and isinstance(data, dict) and "effects_settings" in data:
            data = dict(data)
            data.pop("effects_settings", None)
            log.debug("SignConfig.update_from_dict: dropped wire effects_settings (override active)")

        def _do():
            self.filters = [FilterRule.from_dict(f) for f in data.get("filters", [])]
            self.senders = _parse_senders_wire(data.get("senders", []))
            sign_data = data.get("sign")
            self.sign = SignSettings.from_dict(sign_data) if sign_data else SignSettings()
            self.timezone = data.get("timezone", "US/Pacific")
            self.version = data.get("version", self.CURRENT_VERSION)
            # Only overwrite the new blocks if the incoming payload carries them.
            # This keeps the existing in-memory values when a v1 partial update
            # arrives (the migration fills defaults, so the blocks are present
            # — but we still want the caller's intent to "leave it alone" honored).
            if "effects_settings" in data:
                es = data["effects_settings"]
                self.effects_settings = es if isinstance(es, EffectsSettings) else EffectsSettings.from_dict(es or {})
            if "text_settings" in data:
                ts = data["text_settings"]
                self.text_settings = ts if isinstance(ts, TextSettings) else TextSettings.from_dict(ts or {})

        self._with_lock(_do)
