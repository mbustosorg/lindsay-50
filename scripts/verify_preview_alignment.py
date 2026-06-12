#!/usr/bin/env python3
"""One-off cross-implementation alignment probe (Section 9.9).

Confirms the device's MatrixScroller and the browser's PreviewScroller
produce the same per-tick x-position deltas. Both inherit `tick` from
`lib_shared.scroller_base.ScrollerBase`, so the time/pixel math is
identical by construction — this script exercises that guarantee
explicitly.

Run from the repo root with the venv active:

    PYTHONPATH=. python3 scripts/verify_preview_alignment.py

Exits 0 on success, non-zero on failure. Intended to be run by hand
(not part of CI) since the device side imports rgbmatrix, which is
not installable on a workstation.
"""
import importlib.util
import os
import sys
import time
import types
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT))

# Stub rgbmatrix.graphics so the device-side scroller module imports
# without the hzeller C extension.
_RG = types.ModuleType("rgbmatrix")
_RG.graphics = types.ModuleType("rgbmatrix.graphics")
sys.modules["rgbmatrix"] = _RG
sys.modules["rgbmatrix.graphics"] = _RG.graphics

# Bootstrap config so the device's get_config() call in MatrixScroller
# doesn't blow up (we never reach it, but ScrollerBase.__init__ reads
# frame_delay directly).
os.environ.update({
    "MQTT_CLIENT": "paho", "MQTT_HOST": "localhost", "MQTT_PORT": "1883",
    "MQTT_USERNAME": "test", "MQTT_PASSWORD": "test", "MQTT_TOPIC": "test",
    "AWS_ACCESS_KEY_ID": "test", "AWS_SECRET_ACCESS_KEY": "test",
    "AWS_S3_BUCKET": "test", "AWS_S3_REGION": "us-east-1",
    "CONFIG_API_URL": "http://localhost", "MESSAGES_API_URL": "http://localhost",
    "API_KEY": "test",
})
from lib_shared import config_reader
config_reader._CONFIG_SINGLETON = None
config_reader.get_config({
    "MQTT_CLIENT", "MQTT_HOST", "MQTT_PORT", "MQTT_USERNAME", "MQTT_PASSWORD",
    "MQTT_TOPIC", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_S3_BUCKET",
    "AWS_S3_REGION", "CONFIG_API_URL", "MESSAGES_API_URL", "API_KEY",
})

# Load MatrixScroller via the hyphenated-name loader.
_spec = importlib.util.spec_from_file_location(
    "mc_scroller", str(REPO_ROOT / "heart-matrix-controller" / "scroller.py")
)
_mc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mc)
MatrixScroller, ScrollerBase = _mc.MatrixScroller, _mc.ScrollerBase

# Stub Font (we never actually blit; this is just for set_text width math)
class _StubFont:
    height = 11
    baseline = 9
    def CharacterWidth(self, c):
        return 5

class _StubCanvas:
    width = 64
    height = 64
class _StubDisplay:
    canvas = _StubCanvas()

# Build MatrixScroller without invoking __init__'s font load.
m = MatrixScroller.__new__(MatrixScroller)
ScrollerBase.__init__(m, frame_delay=0.04, offset_seconds=1.0, color=0xFFFFFF)
m.display = _StubDisplay()
m.font = _StubFont()
m.font_height = m.font.height
m.font_baseline = m.font.baseline
m.compute_layout(64, 64)
m.text = None
m.text_width = 0
m.top_x = 0
m.bottom_x = 0
m.brightness = 1.0
m.last_tick = 1000.0

# Load PreviewScroller.
_spec2 = importlib.util.spec_from_file_location(
    "preview_scroller", str(REPO_ROOT / "heart-message-manager" / "preview_scroller.py")
)
_pv = importlib.util.module_from_spec(_spec2)
_spec2.loader.exec_module(_pv)
PreviewScroller = _pv.PreviewScroller

class _PreviewDisplay:
    width = 64
    height = 64

p = PreviewScroller(display=_PreviewDisplay(), color=0xFFFFFF,
                    frame_delay=0.04, offset_seconds=1.0)
p.last_tick = 1000.0

# 1. Both subclasses use ScrollerBase.tick (no override) — the alignment
#    guarantee is structural.
assert MatrixScroller.tick is ScrollerBase.tick, "MatrixScroller must inherit ScrollerBase.tick"
assert PreviewScroller.tick is ScrollerBase.tick, "PreviewScroller must inherit ScrollerBase.tick"
print("OK: both subclasses use ScrollerBase.tick (no override)")

# 2. Mock time.monotonic so both see the same clock.
fake_time = [1000.0]
def fake_monotonic():
    return fake_time[0]
time.monotonic = fake_monotonic
ScrollerBase.tick.__globals__["time"] = time  # patch the import inside scroller_base

# 3. Drive both through identical elapsed time and compare deltas.
m.set_text("hello", 64)
p.set_text("hello", 64)
m.last_tick = fake_time[0]
p.last_tick = fake_time[0]
m_top0, m_bot0 = m.top_x, m.bottom_x
p_top0, p_bot0 = p.top_x, p.bottom_x

# Advance 0.5s of fake time at frame_delay=0.04 (~12.5 frames).
fake_time[0] += 0.5
m.tick(64)
p.tick(64)
m_dtop = m_top0 - m.top_x
m_dbot = m_bot0 - m.bottom_x
p_dtop = p_top0 - p.top_x
p_dbot = p_bot0 - p.bottom_x

print(f"After 0.5s of fake time:")
print(f"  matrix: top_x delta = {m_dtop}, bottom_x delta = {m_dbot}")
print(f"  prev:   top_x delta = {p_dtop}, bottom_x delta = {p_dbot}")

assert m_dtop == p_dtop, f"top_x delta drift: {m_dtop} vs {p_dtop}"
assert m_dbot == p_dbot, f"bottom_x delta drift: {m_dbot} vs {p_dbot}"
print("OK 9.9: cross-implementation alignment verified")
print("     identical per-tick deltas over 0.5s of monotonic time")
sys.exit(0)
