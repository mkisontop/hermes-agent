"""Fix #1 — surface metadata through memory read responses.

The model can already SEE its memory via the system-prompt snapshot and via the
`memory` tool's response. Previously the response exposed only the raw string
entries; metadata (confidence, source, last_seen, supersedes) lived in the
sidecar JSON invisible to the model.

This makes decisions like "should I replace vs add?", "is this stale?",
"did I correct this before?" blind — you're looking at your own notes but
can't see who/when wrote them.

Fix: add an `entries_meta` field to read responses. A list of dicts, one per
entry (same order as `entries`), each with:
    - id (12-char hash)
    - source (user_explicit | user_correction | inferred | legacy)
    - confidence (float)
    - first_seen, last_seen (ISO-8601)
    - evidence_count (int)
    - supersedes (list of prior hash ids)
    - decayed (bool)

Backward compat: `entries` stays `List[str]`. System prompt snapshot stays
unchanged (protects prefix cache).
"""

import json
from pathlib import Path

import pytest

from tools.memory_tool import MemoryStore


@pytest.fixture
def tmp_mem_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    mem_dir = tmp_path / "memories"
    mem_dir.mkdir(parents=True, exist_ok=True)
    return mem_dir


class TestMetadataSurfacing:
    def test_add_response_includes_entries_meta(self, tmp_mem_dir):
        store = MemoryStore()
        store.load_from_disk()
        r = store.add("memory", "MK prefers FastAPI")

        assert r["success"] is True
        assert "entries_meta" in r, "add response must expose entries_meta"
        assert isinstance(r["entries_meta"], list)
        assert len(r["entries_meta"]) == 1

        meta = r["entries_meta"][0]
        assert meta["source"] == "user_explicit"
        assert meta["confidence"] == 1.0
        assert meta["evidence_count"] == 1
        assert meta["decayed"] is False
        assert meta["supersedes"] == []
        assert meta["first_seen"]
        assert meta["last_seen"]
        assert meta["id"], "12-char hash id must be exposed"
        assert len(meta["id"]) == 12

    def test_entries_and_entries_meta_align_by_index(self, tmp_mem_dir):
        store = MemoryStore()
        store.load_from_disk()
        store.add("memory", "first thing", source="user_explicit")
        store.add("memory", "second thing", source="inferred")
        r = store.add("memory", "third thing", source="user_correction")

        assert r["entries"][0] == "first thing"
        assert r["entries"][1] == "second thing"
        assert r["entries"][2] == "third thing"

        # entries_meta[i] describes entries[i]
        assert r["entries_meta"][0]["source"] == "user_explicit"
        assert r["entries_meta"][1]["source"] == "inferred"
        assert r["entries_meta"][2]["source"] == "user_correction"

    def test_read_response_on_load_surfaces_backfilled_legacy(self, tmp_mem_dir):
        # Pre-existing MEMORY.md, no meta — simulates upgrade path
        (tmp_mem_dir / "MEMORY.md").write_text("legacy A\n§\nlegacy B")
        store = MemoryStore()
        store.load_from_disk()

        # Reading back via any mutation (e.g. add) should surface legacy markers
        r = store.add("memory", "fresh one")
        # 3 entries now: 2 legacy + 1 fresh
        assert len(r["entries"]) == 3
        assert len(r["entries_meta"]) == 3

        # Find legacy entries by content → check source=legacy
        for i, content in enumerate(r["entries"]):
            if content.startswith("legacy"):
                assert r["entries_meta"][i]["source"] == "legacy"
                assert r["entries_meta"][i]["confidence"] == 0.7
            elif content == "fresh one":
                assert r["entries_meta"][i]["source"] == "user_explicit"
                assert r["entries_meta"][i]["confidence"] == 1.0

    def test_replace_surfaces_supersedes_chain(self, tmp_mem_dir):
        store = MemoryStore()
        store.load_from_disk()
        store.add("memory", "MK uses Docker Desktop")
        r = store.replace("memory", "Docker Desktop", "MK uses Colima, not Docker Desktop")

        assert r["success"] is True
        # The surviving live entry should show supersedes is non-empty
        # (meta["entries"] keeps superseded rows as decayed=True; live ones filtered)
        live = [m for m in r["entries_meta"] if not m.get("decayed")]
        assert len(live) == 1
        assert live[0]["source"] == "user_correction"
        assert live[0]["supersedes"], "replace must expose supersedes chain"

    def test_evidence_count_visible_on_reassert(self, tmp_mem_dir):
        store = MemoryStore()
        store.load_from_disk()
        store.add("memory", "MK uses Python 3.12")
        r = store.add("memory", "MK uses Python 3.12")  # duplicate — bumps evidence

        # Only one entry in the .md, but evidence_count should be 2
        assert len(r["entries"]) == 1
        assert len(r["entries_meta"]) == 1
        assert r["entries_meta"][0]["evidence_count"] == 2

    def test_system_prompt_snapshot_unchanged(self, tmp_mem_dir):
        """Fix #1 must NOT pollute the system-prompt snapshot — prefix cache.

        The frozen snapshot is rendered from raw entries; metadata goes to tool
        responses only.
        """
        store = MemoryStore()
        store.load_from_disk()
        store.add("memory", "some fact")
        store.load_from_disk()  # refresh snapshot (simulates next session start)

        snap = store.format_for_system_prompt("memory")
        assert snap is not None
        assert "some fact" in snap
        # Metadata markers should NOT appear in the snapshot
        assert "source" not in snap
        assert "confidence" not in snap
        assert "evidence_count" not in snap
        assert "supersedes" not in snap

    def test_entries_field_stays_list_of_strings(self, tmp_mem_dir):
        """Backward compat — existing callers that do `"x" in result["entries"]`
        (exact element match) must keep working."""
        store = MemoryStore()
        store.load_from_disk()
        r = store.add("memory", "Python 3.12 project")

        assert isinstance(r["entries"], list)
        assert all(isinstance(e, str) for e in r["entries"])
        assert "Python 3.12 project" in r["entries"]  # exact-element match
