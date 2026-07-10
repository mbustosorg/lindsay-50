"""Tests that lib_shared.patterns imports cleanly and each pattern constructs.

Verifies every pattern module is importable from its new shared location
and the constructor accepts a stub display with a canvas-like shape.
"""

import importlib
import sys
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))


class _StubCanvas:
    width = 8
    height = 8

    def SetPixel(self, *a, **kw):
        pass

    def SetImage(self, *a, **kw):
        pass


class _StubDisplay:
    canvas = _StubCanvas()


@pytest.mark.parametrize(
    "module_name,class_name",
    [
        ("lib_shared.patterns.fireworks", "Fireworks"),
        ("lib_shared.patterns.nightsky", "NightSky"),
        ("lib_shared.patterns.honeycomb", "Honeycomb"),
        ("lib_shared.patterns.windfire", "WindFire"),
        ("lib_shared.patterns.coronal_mass_ejection", "CoronalMassEjection"),
        ("lib_shared.patterns.eyeball", "Eyeball"),
        ("lib_shared.patterns.marble", "Marble"),
        ("lib_shared.patterns.hyperspace", "Hyperspace"),
        ("lib_shared.patterns.heartbeat", "Heartbeat"),
    ],
)
def test_pattern_module_imports_and_constructs(module_name, class_name):
    mod = importlib.import_module(module_name)
    cls = getattr(mod, class_name)
    assert cls is not None
    instance = cls(_StubDisplay())
    assert instance is not None
    assert hasattr(instance, "tick")
    assert hasattr(instance, "render")
    assert hasattr(instance, "set_brightness")


def test_image_display_is_importable_but_not_in_registry():
    """ImageDisplay is an inner renderer consumed by MediaCycler; it
    is NOT an entry in the effects registry. Verify it can still be
    imported and instantiated (the bare class surface) without going
    through the loader."""
    from lib_shared.patterns.image_display import ImageDisplay

    assert callable(ImageDisplay)
    # The class must NOT appear in the canonical effects list (its
    # work is done indirectly via MediaCycler, which the user never
    # sees in /settings).
    from lib_shared.effects_loader import load_effects_settings

    canonical_names = {e["name"] for e in load_effects_settings()["effects"]}
    assert "ImageDisplay" not in canonical_names


def test_make_effect_class_returns_none_for_legacy_image_display_names():
    """Legacy operator-override entries (PngDisplay, ImageDisplay)
    must land gracefully — make_effect_class returns None + WARNING
    instead of crashing. Verifies a stale operator override doesn't
    take the device down."""
    import lib_shared.effects_loader as effects_loader

    effects_loader.reset_effects_settings()
    for name in ("PngDisplay", "ImageDisplay", "VideoDisplay"):
        result = effects_loader.make_effect_class(name)
        assert result is None, f"make_effect_class({name!r}) returned {result!r}, expected None"


def test_patterns_dont_import_rgb_display():
    """None of the moved pattern modules imports from rgb_display anymore."""
    from lib_shared.patterns import (
        coronal_mass_ejection,
        eyeball,
        fireworks,
        heartbeat,
        honeycomb,
        hyperspace,
        marble,
        nightsky,
        windfire,
    )

    for mod in (
        coronal_mass_ejection,
        eyeball,
        fireworks,
        heartbeat,
        honeycomb,
        hyperspace,
        marble,
        nightsky,
        windfire,
    ):
        assert not hasattr(
            mod, "rgb_display"
        ), f"{mod.__name__} still references rgb_display — should be lib_shared.effect_base"
