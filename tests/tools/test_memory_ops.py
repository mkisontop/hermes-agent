"""Tests for tools/memory_ops.py — stateless memory hygiene operations.

These functions take meta dicts (and entry text dicts) and return analysis
results or mutate in place. No disk I/O — pure functions, easy to unit test.
"""
from datetime import datetime, timedelta, timezone

import pytest

from tools.memory_ops import (
    apply_decay,
    merge_candidates,
    vacuum_candidates,
)


class TestDecay:
    def test_young_entries_no_decay(self):
        meta = {
            "entries": {
                "a": {
                    "confidence": 1.0,
                    "last_seen": datetime.now(timezone.utc).isoformat(),
                    "evidence_count": 1,
                    "decayed": False,
                }
            }
        }
        changed = apply_decay(meta, now=datetime.now(timezone.utc))
        assert changed == 0
        assert meta["entries"]["a"]["confidence"] == 1.0

    def test_30_day_idle_drops_by_0_05(self):
        past = datetime.now(timezone.utc) - timedelta(days=30)
        meta = {
            "entries": {
                "a": {
                    "confidence": 1.0,
                    "last_seen": past.isoformat(),
                    "evidence_count": 1,
                    "decayed": False,
                }
            }
        }
        changed = apply_decay(meta, now=datetime.now(timezone.utc))
        assert changed == 1
        assert meta["entries"]["a"]["confidence"] == pytest.approx(0.95)

    def test_180_day_idle_approaches_floor(self):
        past = datetime.now(timezone.utc) - timedelta(days=180)
        meta = {
            "entries": {
                "a": {
                    "confidence": 0.7,
                    "last_seen": past.isoformat(),
                    "evidence_count": 1,
                    "decayed": False,
                }
            }
        }
        apply_decay(meta, now=datetime.now(timezone.utc))
        assert meta["entries"]["a"]["confidence"] < 0.7

    def test_decay_never_below_zero(self):
        past = datetime.now(timezone.utc) - timedelta(days=3650)
        meta = {
            "entries": {
                "a": {
                    "confidence": 0.1,
                    "last_seen": past.isoformat(),
                    "evidence_count": 1,
                    "decayed": True,
                }
            }
        }
        apply_decay(meta, now=datetime.now(timezone.utc))
        assert meta["entries"]["a"]["confidence"] >= 0.0

    def test_decay_marks_decayed_flag(self):
        past = datetime.now(timezone.utc) - timedelta(days=60)
        meta = {
            "entries": {
                "a": {
                    "confidence": 0.9,
                    "last_seen": past.isoformat(),
                    "evidence_count": 1,
                    "decayed": False,
                }
            }
        }
        apply_decay(meta, now=datetime.now(timezone.utc))
        assert meta["entries"]["a"]["decayed"] is True

    def test_missing_last_seen_skipped(self):
        meta = {
            "entries": {
                "a": {"confidence": 0.9, "evidence_count": 1, "decayed": False}
            }
        }
        changed = apply_decay(meta, now=datetime.now(timezone.utc))
        assert changed == 0

    def test_empty_meta_safe(self):
        meta = {"entries": {}}
        assert apply_decay(meta) == 0


class TestVacuumCandidates:
    def test_low_confidence_flagged_for_deletion(self):
        meta = {
            "entries": {
                "a": {
                    "confidence": 0.15,
                    "last_seen": "2025-01-01T00:00:00+00:00",
                },
                "b": {
                    "confidence": 0.9,
                    "last_seen": "2026-04-01T00:00:00+00:00",
                },
            }
        }
        cands = vacuum_candidates(meta, threshold=0.2)
        assert any(c["id"] == "a" for c in cands)
        assert not any(c["id"] == "b" for c in cands)

    def test_threshold_boundary_excluded(self):
        meta = {
            "entries": {
                "a": {"confidence": 0.2, "last_seen": "2025-01-01T00:00:00+00:00"},
            }
        }
        # 0.2 is NOT below 0.2 — exclusive comparison
        cands = vacuum_candidates(meta, threshold=0.2)
        assert cands == []

    def test_includes_metadata_fields(self):
        meta = {
            "entries": {
                "a": {
                    "confidence": 0.05,
                    "last_seen": "2025-01-01T00:00:00+00:00",
                    "source": "user_explicit",
                }
            }
        }
        cands = vacuum_candidates(meta, threshold=0.2)
        assert len(cands) == 1
        assert cands[0]["source"] == "user_explicit"
        assert cands[0]["confidence"] == 0.05


class TestMergeCandidates:
    def test_overlapping_text_flagged_for_merge(self):
        entries = {
            "aaa": "MK uses Python 3.12 for self-evolution",
            "bbb": "MK runs Python 3.12 in self-evolution venv",
            "ccc": "Docker setup via Colima",
        }
        pairs = merge_candidates(entries, similarity_threshold=0.5)
        ids = {frozenset([p["a"], p["b"]]) for p in pairs}
        assert frozenset(["aaa", "bbb"]) in ids
        assert frozenset(["aaa", "ccc"]) not in ids

    def test_no_pairs_below_threshold(self):
        entries = {
            "a": "completely unrelated text about cats",
            "b": "totally different topic about quantum physics",
        }
        pairs = merge_candidates(entries, similarity_threshold=0.6)
        assert pairs == []

    def test_empty_input_safe(self):
        assert merge_candidates({}) == []
        assert merge_candidates({"only": "one entry"}) == []
