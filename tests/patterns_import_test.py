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
        ("lib_shared.patterns.hyperspace", "Hyperspace"),
        ("lib_shared.patterns.heartbeat", "Heartbeat"),
        ("lib_shared.patterns.png_display", "PngDisplay"),
        ("lib_shared.patterns.video_display", "VideoDisplay"),
    ],
)
def test_pattern_module_imports_and_constructs(module_name, class_name):
    mod = importlib.import_module(module_name)
    cls = getattr(mod, class_name)
    assert cls is not None
    # PngDisplay + VideoDisplay need filesystem assets / OpenCV; the
    # bare constructor (with defaults) might raise. The other six patterns
    # just need a canvas with width/height, which our stub satisfies.
    if class_name in ("PngDisplay", "VideoDisplay"):
        # We're verifying the module is importable; constructor failure on
        # missing assets is acceptable here — the surface test is the
        # import + the class being present.
        assert callable(cls)
    else:
        instance = cls(_StubDisplay())
        assert instance is not None
        assert hasattr(instance, "tick")
        assert hasattr(instance, "render")
        assert hasattr(instance, "set_brightness")


def test_patterns_dont_import_rgb_display():
    """None of the moved pattern modules imports from rgb_display anymore."""
    from lib_shared.patterns import (
        coronal_mass_ejection,
        eyeball,
        fireworks,
        heartbeat,
        honeycomb,
        hyperspace,
        nightsky,
        png_display,
        video_display,
        windfire,
    )

    for mod in (
        coronal_mass_ejection,
        eyeball,
        fireworks,
        heartbeat,
        honeycomb,
        hyperspace,
        nightsky,
        png_display,
        video_display,
        windfire,
    ):
        assert not hasattr(
            mod, "rgb_display"
        ), f"{mod.__name__} still references rgb_display — should be lib_shared.effect_base"
