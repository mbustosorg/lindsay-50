"""Map a canonical effect name (the `name` field in
`EffectsSettings.effects`) to the concrete Effect class.

The class itself isn't material — the EffectsCoordinator only needs a
list of Effect instances, not the classes. The factory exists to keep
import scope tight: each effect's heavy import-time deps (numpy for
Honeycomb, OpenCV for VideoDisplay, PIL for PngDisplay) are paid only
when that effect is actually requested. Tests and the browser preview
can call `make_effect_class("Fireworks")` and never import numpy.
"""

import logging

log = logging.getLogger("heart")


def make_effect_class(name: str) -> type | None:
    """Return the Effect class registered under `name`, or None if unknown.

    The class is loaded on demand; only the requested effect's module
    is imported. Unknown names log a warning and return None so
    `build_effects` can skip them.
    """
    if name == "Hyperspace":
        from lib_shared.patterns.hyperspace import Hyperspace

        return Hyperspace
    if name == "VideoDisplay":
        from lib_shared.patterns.video_display import VideoDisplay

        return VideoDisplay
    if name == "PngDisplay":
        from lib_shared.patterns.png_display import PngDisplay

        return PngDisplay
    if name == "Honeycomb":
        from lib_shared.patterns.honeycomb import Honeycomb

        return Honeycomb
    if name == "Flame":
        from lib_shared.patterns.flame import Flame

        return Flame
    if name == "Fireworks":
        from lib_shared.patterns.fireworks import Fireworks

        return Fireworks
    if name == "NightSky":
        from lib_shared.patterns.nightsky import NightSky

        return NightSky
    log.warning("make_effect_class: unknown effect name %r (skipped)", name)
    return None
