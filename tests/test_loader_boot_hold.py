"""Tests for the loader's `BOOT_HOLD_S` constant.

The constant is now `17.0` — derived as "3× status.json writes
(5s each) + 2s slack" so the loader and the UI read the same
"3 missed writes" signal at the same scale. The 5s cadence is
shared with the MQTT publish path on the device.

The test reads the source file directly to assert on the
value and the comment so a future regression is caught at
the literal level, not just the import level.
"""

from __future__ import annotations

import re
from pathlib import Path


class TestBootHold:
    def test_boot_hold_value_is_17(self):
        # The Pi-side module loader is not a real package — it's a
        # single file in `heart-matrix-controller/`. The conftest
        # adds that directory to sys.path so `import loader` works.
        import loader

        assert hasattr(loader, "BOOT_HOLD_S")
        assert loader.BOOT_HOLD_S == 17.0
        assert isinstance(loader.BOOT_HOLD_S, float)

    def test_loader_source_documents_derivation(self):
        loader_path = Path(
            "/Users/adam/.agent-orchestrator/projects/lindsay-50_f658f025d5/worktrees/l5-19/heart-matrix-controller/loader.py"
        )
        assert loader_path.exists()
        content = loader_path.read_text(encoding="utf-8")
        # Find the BOOT_HOLD_S assignment and the surrounding comment.
        m = re.search(
            r"^(?P<comment>(?:#[^\n]*\n)+)#?\s*BOOT_HOLD_S\s*=\s*17\.0",
            content,
            re.MULTILINE,
        )
        assert m is not None, "BOOT_HOLD_S = 17.0 not found in loader.py"
        comment = m.group("comment")
        # The derivation is documented in the comment.
        assert "3×" in comment or "3x" in comment
        assert "5s" in comment
        assert "2s slack" in comment
        assert "status.json" in comment

    def test_status_writer_default_is_5_seconds(self):
        from status import DEFAULT_TICK_INTERVAL_S

        # Unified cadence: 5s for both file write and MQTT publish.
        assert DEFAULT_TICK_INTERVAL_S == 5.0
