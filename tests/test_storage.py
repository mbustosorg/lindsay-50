"""Tests for lib/storage.py."""

import pytest
from lib.models import Config, FilterRule, Message, RenderingSettings, SignSettings
from lib import storage


class TestStorageMessage:
    def test_put_and_get_message(self, temp_db):
        storage.init_db()
        msg = Message(id="test-001", sender="+15551234567", body="hello", received_at="2026-05-08T10:00:00Z")
        storage.put_message(msg)

        retrieved = storage.get_message("test-001")
        assert retrieved is not None
        assert retrieved.id == "test-001"
        assert retrieved.sender == "+15551234567"
        assert retrieved.body == "hello"

    def test_get_nonexistent_message(self, temp_db):
        storage.init_db()
        assert storage.get_message("does-not-exist") is None

    def test_get_all_messages_ordered_descending(self, temp_db):
        storage.init_db()
        # T{i+1:02d} gives T01, T02, T03
        for i, body in enumerate(["first", "second", "third"]):
            msg = Message(id=f"m{i}", sender="+1555", body=body, received_at=f"2026-05-08T{i+1:02d}:00:00Z")
            storage.put_message(msg)

        all_msgs = storage.get_all_messages()
        ids = [m.id for m in all_msgs]
        # Descending by timestamp: T03 (m2) > T02 (m1) > T01 (m0)
        assert ids == ["m2", "m1", "m0"]

    def test_get_messages_since(self, temp_db):
        storage.init_db()
        msgs = [
            Message(id="m1", sender="+1555", body="a", received_at="2026-05-08T10:00:00Z"),
            Message(id="m2", sender="+1555", body="b", received_at="2026-05-08T11:00:00Z"),
            Message(id="m3", sender="+1555", body="c", received_at="2026-05-08T12:00:00Z"),
        ]
        for m in msgs:
            storage.put_message(m)

        result = storage.get_messages_since("2026-05-08T11:00:00Z")
        ids = [m.id for m in result]
        assert ids == ["m3"]  # only m3 (12:00) is strictly after 11:00

    def test_message_count(self, temp_db):
        storage.init_db()
        assert storage.message_count() == 0
        storage.put_message(Message(id="a", sender="+1", body="x", received_at=""))
        assert storage.message_count() == 1
        storage.put_message(Message(id="b", sender="+1", body="y", received_at=""))
        assert storage.message_count() == 2

    def test_put_message_upsert(self, temp_db):
        storage.init_db()
        msg = Message(id="dup", sender="+1555", body="v1", received_at="2026-05-08T10:00:00Z")
        storage.put_message(msg)

        msg2 = Message(id="dup", sender="+1555", body="v2", received_at="2026-05-08T11:00:00Z")
        storage.put_message(msg2)

        assert storage.message_count() == 1
        assert storage.get_message("dup").body == "v2"


class TestStorageConfig:
    def test_get_config_default_when_empty(self, temp_db):
        storage.init_db()
        cfg = storage.get_config()
        assert cfg.version == 1
        assert cfg.filters == []
        assert cfg.sign.name == "Lindsay's Heart"

    def test_put_and_get_config(self, temp_db):
        storage.init_db()
        cfg = Config(
            version=1,
            allowed_senders=[],
            filters=[FilterRule(type="keyword", pattern="spam", action="suppress")],
            rendering=RenderingSettings(mode="scroll", speed=0.05, color=0xFF0000),
            sign=SignSettings(name="Test Sign"),
        )
        storage.put_config(cfg)

        retrieved = storage.get_config()
        assert len(retrieved.filters) == 1
        assert retrieved.filters[0].type == "keyword"
        assert retrieved.rendering.speed == 0.05
        assert retrieved.sign.name == "Test Sign"

    def test_put_config_upserts(self, temp_db):
        storage.init_db()
        cfg1 = Config(version=1, allowed_senders=[], filters=[], rendering=RenderingSettings(), sign=SignSettings(name="Sign1"))
        storage.put_config(cfg1)

        cfg2 = Config(version=1, allowed_senders=[], filters=[], rendering=RenderingSettings(), sign=SignSettings(name="Sign2"))
        storage.put_config(cfg2)

        # Should still be 1 row
        cfg = storage.get_config()
        assert cfg.sign.name == "Sign2"
