"""Tests for MemoryStore metadata sidecar (MEMORY.meta.json / USER.meta.json).

The model never sees these files. They live next to MEMORY.md / USER.md and
track per-entry provenance: id, confidence, first_seen, last_seen,
evidence_count, scope, supersedes, source, decayed.
"""

import json
from pathlib import Path

import pytest

from tools.memory_tool import MemoryStore


@pytest.fixture
def tmp_mem_dir(tmp_path, monkeypatch):
    """Isolate memory dir per-test by pointing HERMES_HOME at tmp_path.

    get_memory_dir() resolves HERMES_HOME fresh every call, so this is safe.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    mem_dir = tmp_path / "memories"
    mem_dir.mkdir(parents=True, exist_ok=True)
    return mem_dir


class TestMetadataSidecar:
    def test_meta_created_on_first_add(self, tmp_mem_dir):
        store = MemoryStore()
        store.load_from_disk()
        store.add("memory", "MK prefers FastAPI over Flask")

        meta_path = tmp_mem_dir / "MEMORY.meta.json"
        assert meta_path.exists(), "meta sidecar should be written on add"

        meta = json.loads(meta_path.read_text())
        assert meta["version"] == 1
        assert len(meta["entries"]) == 1

        entry = next(iter(meta["entries"].values()))
        assert entry["confidence"] == 1.0  # user_explicit default
        assert entry["first_seen"]
        assert entry["last_seen"]
        assert entry["evidence_count"] == 1
        assert entry["source"] == "user_explicit"
        assert entry["scope"] == "global"
        assert entry["supersedes"] == []
        assert entry["decayed"] is False

    def test_legacy_md_without_meta_backfills(self, tmp_mem_dir):
        # Pre-existing MEMORY.md, no meta file yet (simulates upgrade)
        (tmp_mem_dir / "MEMORY.md").write_text("legacy entry one\n§\nlegacy entry two")

        store = MemoryStore()
        store.load_from_disk()

        meta_path = tmp_mem_dir / "MEMORY.meta.json"
        assert meta_path.exists()
        meta = json.loads(meta_path.read_text())
        assert len(meta["entries"]) == 2

        # Backfilled entries: source=legacy, confidence=0.7
        for e in meta["entries"].values():
            assert e["source"] == "legacy"
            assert e["confidence"] == 0.7
            assert e["evidence_count"] == 1
            assert e["decayed"] is False

    def test_remove_drops_metadata(self, tmp_mem_dir):
        store = MemoryStore()
        store.load_from_disk()
        store.add("memory", "thing one")
        store.add("memory", "thing two")
        store.remove("memory", "thing one")

        meta = json.loads((tmp_mem_dir / "MEMORY.meta.json").read_text())
        assert len(meta["entries"]) == 1
        # The surviving entry should be "thing two"
        surviving = next(iter(meta["entries"].values()))
        # We don't expose content in meta, but content_hash should match thing two
        import hashlib
        expected = hashlib.sha256("thing two".encode("utf-8")).hexdigest()
        assert surviving["content_hash"] == expected

    def test_replace_creates_supersedes_chain(self, tmp_mem_dir):
        store = MemoryStore()
        store.load_from_disk()
        store.add("memory", "MK uses Docker Desktop")
        store.replace("memory", "Docker Desktop", "MK uses Colima, not Docker Desktop")

        meta = json.loads((tmp_mem_dir / "MEMORY.meta.json").read_text())
        live = [e for e in meta["entries"].values() if not e.get("decayed")]
        assert len(live) == 1
        # Replacement is a user_correction event
        assert live[0]["source"] == "user_correction"
        assert live[0]["confidence"] == 1.0
        assert live[0]["supersedes"], "supersedes should be non-empty after replace"

    def test_evidence_count_increments_on_re_add(self, tmp_mem_dir):
        store = MemoryStore()
        store.load_from_disk()
        store.add("memory", "MK uses Python 3.12")
        store.add("memory", "MK uses Python 3.12")  # duplicate

        # The .md file rejects dupes, but meta tracks the re-assertion
        meta = json.loads((tmp_mem_dir / "MEMORY.meta.json").read_text())
        assert len(meta["entries"]) == 1
        entry = next(iter(meta["entries"].values()))
        assert entry["evidence_count"] == 2
        # Confidence bumps on re-evidence, capped at 1.0
        assert entry["confidence"] == 1.0

    def test_secret_blocked_before_disk_write(self, tmp_mem_dir):
        """Secrets should be rejected before any meta is written."""
        store = MemoryStore()
        store.load_from_disk()
        # ghp_ token pattern (40 chars after prefix)
        r = store.add("memory", "my github token is ghp_" + "B" * 40)

        assert r["success"] is False
        assert "secret" in r["error"].lower() or "Blocked" in r["error"]

        meta_path = tmp_mem_dir / "MEMORY.meta.json"
        if meta_path.exists():
            meta = json.loads(meta_path.read_text())
            assert len(meta["entries"]) == 0

    def test_user_target_has_separate_sidecar(self, tmp_mem_dir):
        """USER.md gets USER.meta.json, MEMORY.md gets MEMORY.meta.json."""
        store = MemoryStore()
        store.load_from_disk()
        store.add("user", "user prefers concise answers")
        store.add("memory", "project uses FastAPI")

        user_meta_path = tmp_mem_dir / "USER.meta.json"
        mem_meta_path = tmp_mem_dir / "MEMORY.meta.json"
        assert user_meta_path.exists()
        assert mem_meta_path.exists()

        user_meta = json.loads(user_meta_path.read_text())
        mem_meta = json.loads(mem_meta_path.read_text())
        assert len(user_meta["entries"]) == 1
        assert len(mem_meta["entries"]) == 1
