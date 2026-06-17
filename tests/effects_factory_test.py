"""Tests for lib_shared.effects_factory.make_effect_class.

The factory's contract is narrow: given a canonical effect name, return
the concrete Effect class (or None for unknown). The per-name import
scope is the whole point — tests must NOT need numpy / cv2 / PIL just
to resolve a name, so we only ask for effects whose modules have
import-safe top-level deps (no numpy etc.).
"""

import logging
import sys
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from lib_shared.effects_factory import make_effect_class


def test_factory_resolves_browser_safe_effects():
    """Fireworks, Flame, Hyperspace, NightSky have no heavy top-level
    deps and resolve cleanly without numpy / cv2 / PIL installed."""
    for name in ("Fireworks", "Flame", "Hyperspace", "NightSky"):
        cls = make_effect_class(name)
        assert cls is not None, f"{name!r} did not resolve"
        # The class is importable, has a class name matching the input,
        # and is callable (i.e. we can instantiate it).
        assert cls.__name__ == name
        assert callable(cls)


def test_factory_returns_none_for_unknown():
    """Unknown names return None (logged as a warning) so build_effects
    can filter them silently."""
    cls = make_effect_class("NotARealEffect")
    assert cls is None


def test_factory_logs_warning_for_unknown(caplog):
    """Unknown names emit a warning — config drift is visible in logs."""
    with caplog.at_level(logging.WARNING, logger="heart"):
        cls = make_effect_class("NotARealEffect")
    assert cls is None
    assert any("NotARealEffect" in rec.message for rec in caplog.records)


def test_factory_repeated_calls_no_leak():
    """Calling the factory twice for the same name is fine and idempotent.
    (The function is pure dispatch — no state to leak.)"""
    cls1 = make_effect_class("Fireworks")
    cls2 = make_effect_class("Fireworks")
    assert cls1 is cls2
