"""Memory hygiene operations — stateless, pure functions over meta + entry data.

Keep this module dependency-free (no MemoryStore, no disk I/O) so it's easy to
unit-test and reuse from cron scripts.

Functions:
- apply_decay: lower confidence on idle entries
- vacuum_candidates: list low-confidence entries (deletion proposals)
- merge_candidates: list pairs of similar entries (merge proposals)
"""
from __future__ import annotations

from datetime import datetime, timezone
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional

# Decay tuning — exposed as module constants so cron scripts / tests can monkeypatch.
DECAY_RATE_PER_30D = 0.05
DECAY_FLOOR = 0.0
DECAY_IDLE_DAYS = 30  # entries younger than this are not decayed

# Default thresholds for vacuum and merge proposals.
DEFAULT_VACUUM_THRESHOLD = 0.2
DEFAULT_MERGE_SIMILARITY = 0.6


def _days_since(iso: str, now: datetime) -> float:
    try:
        dt = datetime.fromisoformat(iso)
    except (ValueError, TypeError):
        return 0.0
    return max(0.0, (now - dt).total_seconds() / 86400.0)


def apply_decay(meta: Dict[str, Any], now: Optional[datetime] = None) -> int:
    """Mutate meta in place, decaying confidence for idle entries.

    Returns number of entries that changed.
    Formula: conf -= DECAY_RATE_PER_30D * (days_idle / 30), floor at DECAY_FLOOR.
    Entries idle less than DECAY_IDLE_DAYS or missing last_seen are skipped.
    """
    now = now or datetime.now(timezone.utc)
    changed = 0
    for _eid, entry in meta.get("entries", {}).items():
        last_seen = entry.get("last_seen")
        if not last_seen:
            continue
        days_idle = _days_since(last_seen, now)
        if days_idle < DECAY_IDLE_DAYS:
            continue
        old_conf = float(entry.get("confidence", 0.7))
        decrement = DECAY_RATE_PER_30D * (days_idle / 30.0)
        new_conf = max(DECAY_FLOOR, round(old_conf - decrement, 3))
        if new_conf != old_conf:
            entry["confidence"] = new_conf
            entry["decayed"] = True
            changed += 1
    return changed


def vacuum_candidates(
    meta: Dict[str, Any], threshold: float = DEFAULT_VACUUM_THRESHOLD
) -> List[Dict[str, Any]]:
    """Return entries with confidence strictly below threshold — deletion proposals."""
    out = []
    for eid, entry in meta.get("entries", {}).items():
        conf = float(entry.get("confidence", 1.0))
        if conf < threshold:
            out.append(
                {
                    "id": eid,
                    "confidence": entry.get("confidence"),
                    "last_seen": entry.get("last_seen"),
                    "source": entry.get("source"),
                }
            )
    return out


def merge_candidates(
    entries_by_id: Dict[str, str],
    similarity_threshold: float = DEFAULT_MERGE_SIMILARITY,
) -> List[Dict[str, Any]]:
    """Find pairs of entries with >= threshold text similarity. Merge proposals."""
    items = list(entries_by_id.items())
    pairs = []
    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            ida, ta = items[i]
            idb, tb = items[j]
            ratio = SequenceMatcher(None, ta.lower(), tb.lower()).ratio()
            if ratio >= similarity_threshold:
                pairs.append(
                    {
                        "a": ida,
                        "b": idb,
                        "similarity": round(ratio, 3),
                        "text_a": ta,
                        "text_b": tb,
                    }
                )
    return pairs
