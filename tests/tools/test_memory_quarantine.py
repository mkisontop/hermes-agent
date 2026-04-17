"""Tests for quarantine-on-contradiction in MemoryStore.replace()."""
import json

import pytest

from tools.memory_tool import MemoryStore


@pytest.fixture
def tmp_mem_dir(tmp_path, monkeypatch):
    """Point HERMES_HOME at a temp dir so memories land in isolation."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    mem_dir = tmp_path / "memories"
    mem_dir.mkdir(parents=True, exist_ok=True)
    return mem_dir


class TestQuarantine:
    def test_high_confidence_replace_quarantines_old(self, tmp_mem_dir):
        """High-conf entry (user_explicit, recent) → old lands in quarantine.md."""
        store = MemoryStore()
        store.load_from_disk()
        store.add("memory", "MK uses Docker Desktop")  # source=user_explicit -> 1.0
        store.replace(
            "memory", "Docker Desktop", "MK uses Colima not Docker Desktop"
        )

        q_path = tmp_mem_dir / "MEMORY.quarantine.md"
        assert q_path.exists(), "quarantine file should be created"
        q_text = q_path.read_text(encoding="utf-8")
        assert "MK uses Docker Desktop" in q_text
        assert "reason=contradiction" in q_text

        meta = json.loads(
            (tmp_mem_dir / "MEMORY.meta.json").read_text(encoding="utf-8")
        )
        assert len(meta["quarantine_log"]) == 1
        entry = meta["quarantine_log"][0]
        assert entry["reason"] == "contradiction"
        assert entry["conflicts_with"]  # new content hash recorded

    def test_low_confidence_replace_does_not_quarantine(self, tmp_mem_dir):
        """Low-conf entry (agent_inference → 0.5) → no quarantine drama."""
        store = MemoryStore()
        store.load_from_disk()
        store.add("memory", "MK might use Docker", source="agent_inference")
        store.replace("memory", "might use Docker", "MK actually uses Colima")

        q_path = tmp_mem_dir / "MEMORY.quarantine.md"
        assert (not q_path.exists()) or q_path.read_text().strip() == "", (
            "low-confidence entries should not quarantine"
        )

        meta = json.loads(
            (tmp_mem_dir / "MEMORY.meta.json").read_text(encoding="utf-8")
        )
        assert meta["quarantine_log"] == []

    def test_new_content_still_lands_in_memory_md(self, tmp_mem_dir):
        """Quarantine doesn't interfere with the primary write path."""
        store = MemoryStore()
        store.load_from_disk()
        store.add("memory", "old fact about docker")
        store.replace("memory", "old fact about docker", "new fact about colima")

        md = (tmp_mem_dir / "MEMORY.md").read_text(encoding="utf-8")
        assert "new fact about colima" in md
        assert "old fact about docker" not in md

    def test_user_target_uses_separate_quarantine_file(self, tmp_mem_dir):
        """USER.quarantine.md for user target, MEMORY.quarantine.md for memory."""
        store = MemoryStore()
        store.load_from_disk()
        store.add("user", "MK prefers verbose output")
        store.replace("user", "verbose output", "MK prefers terse output")

        user_q = tmp_mem_dir / "USER.quarantine.md"
        mem_q = tmp_mem_dir / "MEMORY.quarantine.md"
        assert user_q.exists()
        assert "MK prefers verbose output" in user_q.read_text(encoding="utf-8")
        assert not mem_q.exists()

    def test_stale_high_confidence_does_not_quarantine(self, tmp_mem_dir, monkeypatch):
        """High-conf but >7 days old → no quarantine (it's just an update)."""
        from datetime import datetime, timedelta, timezone

        store = MemoryStore()
        store.load_from_disk()
        store.add("memory", "MK uses Intel Mac")

        # Rewind last_seen to 30 days ago
        meta = store._load_meta("memory")
        for h, entry in meta["entries"].items():
            entry["last_seen"] = (
                datetime.now(timezone.utc) - timedelta(days=30)
            ).isoformat()
        store._save_meta("memory", meta)

        store.replace("memory", "Intel Mac", "MK uses Apple Silicon Mac")

        q_path = tmp_mem_dir / "MEMORY.quarantine.md"
        assert (not q_path.exists()) or q_path.read_text().strip() == "", (
            "stale entries should not quarantine even if high-conf"
        )
