#!/usr/bin/env python3
"""
Memory Tool Module - Persistent Curated Memory

Provides bounded, file-backed memory that persists across sessions. Two stores:
  - MEMORY.md: agent's personal notes and observations (environment facts, project
    conventions, tool quirks, things learned)
  - USER.md: what the agent knows about the user (preferences, communication style,
    expectations, workflow habits)

Both are injected into the system prompt as a frozen snapshot at session start.
Mid-session writes update files on disk immediately (durable) but do NOT change
the system prompt -- this preserves the prefix cache for the entire session.
The snapshot refreshes on the next session start.

Entry delimiter: § (section sign). Entries can be multiline.
Character limits (not tokens) because char counts are model-independent.

Design:
- Single `memory` tool with action parameter: add, replace, remove, read
- replace/remove use short unique substring matching (not full text or IDs)
- Behavioral guidance lives in the tool schema description
- Frozen snapshot pattern: system prompt is stable, tool responses show live state
"""

import hashlib
import json
import logging
import os
import re
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from hermes_constants import get_hermes_home
from typing import Dict, Any, List, Optional

# fcntl is Unix-only; on Windows use msvcrt for file locking
msvcrt = None
try:
    import fcntl
except ImportError:
    fcntl = None
    try:
        import msvcrt
    except ImportError:
        pass

logger = logging.getLogger(__name__)

# Where memory files live — resolved dynamically so profile overrides
# (HERMES_HOME env var changes) are always respected.  The old module-level
# constant was cached at import time and could go stale if a profile switch
# happened after the first import.
def get_memory_dir() -> Path:
    """Return the profile-scoped memories directory."""
    return get_hermes_home() / "memories"

ENTRY_DELIMITER = "\n§\n"

# ---------------------------------------------------------------------------
# Metadata sidecar — invisible to the model.
#
# Lives next to MEMORY.md / USER.md as MEMORY.meta.json / USER.meta.json and
# tracks per-entry provenance (id, confidence, first_seen, last_seen,
# evidence_count, scope, supersedes, source, decayed). The model never reads
# this file; only MemoryOps / decay / vacuum / summarizer scripts do.
# ---------------------------------------------------------------------------

META_VERSION = 1

# Default confidence per source. Tune these in one place.
_DEFAULT_CONFIDENCE_BY_SOURCE = {
    "user_explicit": 1.0,
    "user_correction": 1.0,
    "session_summary": 0.6,
    "agent_inference": 0.5,
    "legacy": 0.7,
}


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _hash_id(content: str) -> str:
    """Short stable id derived from entry content (first 12 hex chars of sha256)."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:12]


def _full_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Memory content scanning — lightweight check for injection/exfiltration
# in content that gets injected into the system prompt.
# ---------------------------------------------------------------------------

# Threat + secret scanning live in tools/secret_scanner.py so the summarizer,
# vacuum, and any other callers can reuse the same patterns without a circular
# import against MemoryStore. Keep this file the *consumer*, not the source of truth.
try:
    from tools.secret_scanner import scan_for_threats, scan_for_secrets
except ImportError:
    # Fallback for direct-module execution (e.g. pytest from tools/ dir)
    from secret_scanner import scan_for_threats, scan_for_secrets  # type: ignore


def _scan_memory_content(content: str) -> Optional[str]:
    """Block prompt-injection, exfil, invisible-unicode, AND secret leakage.

    Single gate used by both add() and replace(). Returns error string on block,
    None if clean. Secrets are a hard block — we do not silently redact, because
    a user pasting a live key into memory needs to *know* it was rejected so they
    can rotate it.
    """
    threat_err = scan_for_threats(content)
    if threat_err:
        return threat_err

    hits = scan_for_secrets(content)
    if hits:
        kinds = sorted({h["kind"] for h in hits})
        return (
            f"Blocked: content contains {len(hits)} secret-shaped token(s) "
            f"({', '.join(kinds)}). Memory is injected into the system prompt — "
            f"never store live credentials. Rotate the leaked token(s) and retry "
            f"with a redacted version."
        )

    return None


class MemoryStore:
    """
    Bounded curated memory with file persistence. One instance per AIAgent.

    Maintains two parallel states:
      - _system_prompt_snapshot: frozen at load time, used for system prompt injection.
        Never mutated mid-session. Keeps prefix cache stable.
      - memory_entries / user_entries: live state, mutated by tool calls, persisted to disk.
        Tool responses always reflect this live state.
    """

    def __init__(self, memory_char_limit: int = 2200, user_char_limit: int = 1375):
        self.memory_entries: List[str] = []
        self.user_entries: List[str] = []
        self.memory_char_limit = memory_char_limit
        self.user_char_limit = user_char_limit
        # Frozen snapshot for system prompt -- set once at load_from_disk()
        self._system_prompt_snapshot: Dict[str, str] = {"memory": "", "user": ""}

    def load_from_disk(self):
        """Load entries from MEMORY.md and USER.md, capture system prompt snapshot."""
        mem_dir = get_memory_dir()
        mem_dir.mkdir(parents=True, exist_ok=True)

        self.memory_entries = self._read_file(mem_dir / "MEMORY.md")
        self.user_entries = self._read_file(mem_dir / "USER.md")

        # Deduplicate entries (preserves order, keeps first occurrence)
        self.memory_entries = list(dict.fromkeys(self.memory_entries))
        self.user_entries = list(dict.fromkeys(self.user_entries))

        # Capture frozen snapshot for system prompt injection
        self._system_prompt_snapshot = {
            "memory": self._render_block("memory", self.memory_entries),
            "user": self._render_block("user", self.user_entries),
        }

        # Backfill metadata for any entries without a sidecar record
        try:
            self._backfill_meta("memory")
            self._backfill_meta("user")
        except Exception as e:
            logger.warning("Meta backfill failed (non-fatal): %s", e)

    @staticmethod
    @contextmanager
    def _file_lock(path: Path):
        """Acquire an exclusive file lock for read-modify-write safety.

        Uses a separate .lock file so the memory file itself can still be
        atomically replaced via os.replace().
        """
        lock_path = path.with_suffix(path.suffix + ".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)

        if fcntl is None and msvcrt is None:
            yield
            return

        if msvcrt and (not lock_path.exists() or lock_path.stat().st_size == 0):
            lock_path.write_text(" ", encoding="utf-8")

        fd = open(lock_path, "r+" if msvcrt else "a+")
        try:
            if fcntl:
                fcntl.flock(fd, fcntl.LOCK_EX)
            else:
                fd.seek(0)
                msvcrt.locking(fd.fileno(), msvcrt.LK_LOCK, 1)
            yield
        finally:
            if fcntl:
                fcntl.flock(fd, fcntl.LOCK_UN)
            elif msvcrt:
                try:
                    fd.seek(0)
                    msvcrt.locking(fd.fileno(), msvcrt.LK_UNLCK, 1)
                except (OSError, IOError):
                    pass
            fd.close()

    @staticmethod
    def _path_for(target: str) -> Path:
        mem_dir = get_memory_dir()
        if target == "user":
            return mem_dir / "USER.md"
        return mem_dir / "MEMORY.md"

    def _reload_target(self, target: str):
        """Re-read entries from disk into in-memory state.

        Called under file lock to get the latest state before mutating.
        """
        fresh = self._read_file(self._path_for(target))
        fresh = list(dict.fromkeys(fresh))  # deduplicate
        self._set_entries(target, fresh)

    def save_to_disk(self, target: str):
        """Persist entries to the appropriate file. Called after every mutation."""
        get_memory_dir().mkdir(parents=True, exist_ok=True)
        self._write_file(self._path_for(target), self._entries_for(target))

    def _entries_for(self, target: str) -> List[str]:
        if target == "user":
            return self.user_entries
        return self.memory_entries

    def _set_entries(self, target: str, entries: List[str]):
        if target == "user":
            self.user_entries = entries
        else:
            self.memory_entries = entries

    def _char_count(self, target: str) -> int:
        entries = self._entries_for(target)
        if not entries:
            return 0
        return len(ENTRY_DELIMITER.join(entries))

    def _char_limit(self, target: str) -> int:
        if target == "user":
            return self.user_char_limit
        return self.memory_char_limit

    # -----------------------------------------------------------------
    # Metadata sidecar (MEMORY.meta.json / USER.meta.json)
    # -----------------------------------------------------------------

    def _meta_path(self, target: str) -> Path:
        mem_dir = get_memory_dir()
        return mem_dir / ("USER.meta.json" if target == "user" else "MEMORY.meta.json")

    def _empty_meta(self) -> Dict[str, Any]:
        return {"version": META_VERSION, "entries": {}, "quarantine_log": []}

    def _load_meta(self, target: str) -> Dict[str, Any]:
        p = self._meta_path(target)
        if not p.exists():
            return self._empty_meta()
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            # Future migration hook — for v1 we just ensure required keys exist
            data.setdefault("version", META_VERSION)
            data.setdefault("entries", {})
            data.setdefault("quarantine_log", [])
            return data
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Corrupt meta file %s (%s), starting fresh", p, e)
            return self._empty_meta()

    def _save_meta(self, target: str, meta: Dict[str, Any]):
        p = self._meta_path(target)
        p.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(p.parent), suffix=".tmp", prefix=".meta_")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(meta, f, indent=2, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, str(p))
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def _backfill_meta(self, target: str):
        """Ensure every entry in the .md has a meta record. Missing ones get source=legacy."""
        entries = self._entries_for(target)
        meta = self._load_meta(target)
        changed = False
        for text in entries:
            h = _hash_id(text)
            if h not in meta["entries"]:
                now = _iso_now()
                meta["entries"][h] = {
                    "content_hash": _full_hash(text),
                    "confidence": _DEFAULT_CONFIDENCE_BY_SOURCE["legacy"],
                    "first_seen": now,
                    "last_seen": now,
                    "evidence_count": 1,
                    "scope": "global",
                    "source": "legacy",
                    "supersedes": [],
                    "decayed": False,
                }
                changed = True
        if changed or not self._meta_path(target).exists():
            # Write at least once on first upgrade even if nothing was added,
            # but only if the .md file actually exists (otherwise we'd create
            # an empty meta for a nonexistent store).
            if entries or self._meta_path(target).exists():
                self._save_meta(target, meta)

    def _meta_add_entry(
        self,
        target: str,
        content: str,
        source: str = "user_explicit",
        confidence: Optional[float] = None,
        supersedes: Optional[List[str]] = None,
    ):
        """Record a new entry or bump evidence on a re-assertion."""
        meta = self._load_meta(target)
        h = _hash_id(content)
        now = _iso_now()
        if h in meta["entries"]:
            # Re-assertion — duplicate in the .md, but still a signal.
            e = meta["entries"][h]
            e["evidence_count"] = int(e.get("evidence_count", 1)) + 1
            e["last_seen"] = now
            # Bump confidence, capped at 1.0
            e["confidence"] = min(1.0, float(e.get("confidence", 0.7)) + 0.05)
        else:
            resolved_conf = (
                confidence
                if confidence is not None
                else _DEFAULT_CONFIDENCE_BY_SOURCE.get(source, 0.6)
            )
            meta["entries"][h] = {
                "content_hash": _full_hash(content),
                "confidence": resolved_conf,
                "first_seen": now,
                "last_seen": now,
                "evidence_count": 1,
                "scope": "global",
                "source": source,
                "supersedes": list(supersedes) if supersedes else [],
                "decayed": False,
            }
        self._save_meta(target, meta)

    def _meta_remove_entry(self, target: str, content: str):
        meta = self._load_meta(target)
        h = _hash_id(content)
        if h in meta["entries"]:
            del meta["entries"][h]
            self._save_meta(target, meta)

    def _meta_replace_entry(
        self,
        target: str,
        old_content: str,
        new_content: str,
        source: str = "user_correction",
    ):
        old_h = _hash_id(old_content)
        new_h = _hash_id(new_content)
        meta = self._load_meta(target)
        old_entry = meta["entries"].get(old_h)
        now = _iso_now()

        if new_h in meta["entries"]:
            # New content already tracked — just link supersedes and bump last_seen
            if old_h and old_h != new_h and old_h not in meta["entries"][new_h]["supersedes"]:
                meta["entries"][new_h]["supersedes"].append(old_h)
            meta["entries"][new_h]["last_seen"] = now
        else:
            first_seen = old_entry["first_seen"] if old_entry else now
            meta["entries"][new_h] = {
                "content_hash": _full_hash(new_content),
                "confidence": _DEFAULT_CONFIDENCE_BY_SOURCE.get(source, 1.0),
                "first_seen": first_seen,
                "last_seen": now,
                "evidence_count": 1,
                "scope": (old_entry or {}).get("scope", "global"),
                "source": source,
                "supersedes": [old_h] if (old_h and old_h != new_h) else [],
                "decayed": False,
            }
        # Drop the old record (supersedes chain is preserved on the new entry)
        if old_h in meta["entries"] and old_h != new_h:
            del meta["entries"][old_h]
        self._save_meta(target, meta)

    def _quarantine_entry(
        self,
        target: str,
        content: str,
        reason: str,
        conflicts_with: Optional[str] = None,
    ) -> None:
        """Append the old entry to <TARGET>.quarantine.md and log it in meta.

        Used when replace() overwrites a high-confidence, recent entry — we
        preserve the old fact for the weekly vacuum to review instead of
        silently dropping it.
        """
        mem_dir = get_memory_dir()
        if target == "user":
            q_path = mem_dir / "USER.quarantine.md"
        else:
            q_path = mem_dir / "MEMORY.quarantine.md"

        stamp = _iso_now()
        block = f"\n<!-- quarantined {stamp} reason={reason} -->\n{content}\n"
        try:
            q_path.parent.mkdir(parents=True, exist_ok=True)
            with open(q_path, "a", encoding="utf-8") as f:
                f.write(block)
        except OSError as e:
            logger.warning("Failed to write quarantine file %s: %s", q_path, e)
            return

        meta = self._load_meta(target)
        meta.setdefault("quarantine_log", []).append({
            "id": _hash_id(content),
            "reason": reason,
            "quarantined_at": stamp,
            "conflicts_with": conflicts_with,
        })
        self._save_meta(target, meta)

    def add(self, target: str, content: str, source: str = "user_explicit") -> Dict[str, Any]:
        """Append a new entry. Returns error if it would exceed the char limit."""
        content = content.strip()
        if not content:
            return {"success": False, "error": "Content cannot be empty."}

        # Scan for injection/exfiltration before accepting
        scan_error = _scan_memory_content(content)
        if scan_error:
            return {"success": False, "error": scan_error}

        with self._file_lock(self._path_for(target)):
            # Re-read from disk under lock to pick up writes from other sessions
            self._reload_target(target)

            entries = self._entries_for(target)
            limit = self._char_limit(target)

            # Reject exact duplicates (but still bump evidence)
            if content in entries:
                self._meta_add_entry(target, content)
                return self._success_response(target, "Entry already exists (no duplicate added).")

            # Calculate what the new total would be
            new_entries = entries + [content]
            new_total = len(ENTRY_DELIMITER.join(new_entries))

            if new_total > limit:
                current = self._char_count(target)
                return {
                    "success": False,
                    "error": (
                        f"Memory at {current:,}/{limit:,} chars. "
                        f"Adding this entry ({len(content)} chars) would exceed the limit. "
                        f"Replace or remove existing entries first."
                    ),
                    "current_entries": entries,
                    "usage": f"{current:,}/{limit:,}",
                }

            entries.append(content)
            self._set_entries(target, entries)
            self.save_to_disk(target)
            self._meta_add_entry(target, content, source=source)

        return self._success_response(target, "Entry added.")

    def replace(self, target: str, old_text: str, new_content: str) -> Dict[str, Any]:
        """Find entry containing old_text substring, replace it with new_content."""
        old_text = old_text.strip()
        new_content = new_content.strip()
        if not old_text:
            return {"success": False, "error": "old_text cannot be empty."}
        if not new_content:
            return {"success": False, "error": "new_content cannot be empty. Use 'remove' to delete entries."}

        # Scan replacement content for injection/exfiltration
        scan_error = _scan_memory_content(new_content)
        if scan_error:
            return {"success": False, "error": scan_error}

        with self._file_lock(self._path_for(target)):
            self._reload_target(target)

            entries = self._entries_for(target)
            matches = [(i, e) for i, e in enumerate(entries) if old_text in e]

            if not matches:
                return {"success": False, "error": f"No entry matched '{old_text}'."}

            if len(matches) > 1:
                # If all matches are identical (exact duplicates), operate on the first one
                unique_texts = set(e for _, e in matches)
                if len(unique_texts) > 1:
                    previews = [e[:80] + ("..." if len(e) > 80 else "") for _, e in matches]
                    return {
                        "success": False,
                        "error": f"Multiple entries matched '{old_text}'. Be more specific.",
                        "matches": previews,
                    }
                # All identical -- safe to replace just the first

            idx = matches[0][0]
            old_entry_text = entries[idx]
            limit = self._char_limit(target)

            # Check that replacement doesn't blow the budget
            test_entries = entries.copy()
            test_entries[idx] = new_content
            new_total = len(ENTRY_DELIMITER.join(test_entries))

            if new_total > limit:
                return {
                    "success": False,
                    "error": (
                        f"Replacement would put memory at {new_total:,}/{limit:,} chars. "
                        f"Shorten the new content or remove other entries first."
                    ),
                }

            entries[idx] = new_content
            self._set_entries(target, entries)
            self.save_to_disk(target)

            # Quarantine decision: was the old entry high-confidence AND recent?
            # If so, preserve it for weekly vacuum review instead of silently
            # overwriting. Thresholds: confidence >= 0.8, age <= 7 days.
            try:
                from datetime import datetime, timedelta, timezone

                meta_before = self._load_meta(target)
                old_h = _hash_id(old_entry_text)
                old_rec = meta_before.get("entries", {}).get(old_h)
                if old_rec:
                    conf = float(old_rec.get("confidence", 0.0))
                    last_seen_str = old_rec.get("last_seen")
                    is_recent = False
                    if last_seen_str:
                        try:
                            last_seen = datetime.fromisoformat(last_seen_str)
                            age = datetime.now(timezone.utc) - last_seen
                            is_recent = age <= timedelta(days=7)
                        except ValueError:
                            pass
                    if conf >= 0.8 and is_recent:
                        self._quarantine_entry(
                            target,
                            old_entry_text,
                            reason="contradiction",
                            conflicts_with=_hash_id(new_content),
                        )
            except Exception as e:
                logger.warning("Quarantine check failed (non-fatal): %s", e)

            self._meta_replace_entry(target, old_entry_text, new_content)

        return self._success_response(target, "Entry replaced.")

    def remove(self, target: str, old_text: str) -> Dict[str, Any]:
        """Remove the entry containing old_text substring."""
        old_text = old_text.strip()
        if not old_text:
            return {"success": False, "error": "old_text cannot be empty."}

        with self._file_lock(self._path_for(target)):
            self._reload_target(target)

            entries = self._entries_for(target)
            matches = [(i, e) for i, e in enumerate(entries) if old_text in e]

            if not matches:
                return {"success": False, "error": f"No entry matched '{old_text}'."}

            if len(matches) > 1:
                # If all matches are identical (exact duplicates), remove the first one
                unique_texts = set(e for _, e in matches)
                if len(unique_texts) > 1:
                    previews = [e[:80] + ("..." if len(e) > 80 else "") for _, e in matches]
                    return {
                        "success": False,
                        "error": f"Multiple entries matched '{old_text}'. Be more specific.",
                        "matches": previews,
                    }
                # All identical -- safe to remove just the first

            idx = matches[0][0]
            removed_text = entries[idx]
            entries.pop(idx)
            self._set_entries(target, entries)
            self.save_to_disk(target)
            self._meta_remove_entry(target, removed_text)

        return self._success_response(target, "Entry removed.")

    def format_for_system_prompt(self, target: str) -> Optional[str]:
        """
        Return the frozen snapshot for system prompt injection.

        This returns the state captured at load_from_disk() time, NOT the live
        state. Mid-session writes do not affect this. This keeps the system
        prompt stable across all turns, preserving the prefix cache.

        Returns None if the snapshot is empty (no entries at load time).
        """
        block = self._system_prompt_snapshot.get(target, "")
        return block if block else None

    # -- Internal helpers --

    def _success_response(self, target: str, message: str = None) -> Dict[str, Any]:
        entries = self._entries_for(target)
        current = self._char_count(target)
        limit = self._char_limit(target)
        pct = min(100, int((current / limit) * 100)) if limit > 0 else 0

        resp = {
            "success": True,
            "target": target,
            "entries": entries,
            "usage": f"{pct}% — {current:,}/{limit:,} chars",
            "entry_count": len(entries),
        }
        if message:
            resp["message"] = message
        return resp

    def _render_block(self, target: str, entries: List[str]) -> str:
        """Render a system prompt block with header and usage indicator."""
        if not entries:
            return ""

        limit = self._char_limit(target)
        content = ENTRY_DELIMITER.join(entries)
        current = len(content)
        pct = min(100, int((current / limit) * 100)) if limit > 0 else 0

        if target == "user":
            header = f"USER PROFILE (who the user is) [{pct}% — {current:,}/{limit:,} chars]"
        else:
            header = f"MEMORY (your personal notes) [{pct}% — {current:,}/{limit:,} chars]"

        separator = "═" * 46
        return f"{separator}\n{header}\n{separator}\n{content}"

    @staticmethod
    def _read_file(path: Path) -> List[str]:
        """Read a memory file and split into entries.

        No file locking needed: _write_file uses atomic rename, so readers
        always see either the previous complete file or the new complete file.
        """
        if not path.exists():
            return []
        try:
            raw = path.read_text(encoding="utf-8")
        except (OSError, IOError):
            return []

        if not raw.strip():
            return []

        # Use ENTRY_DELIMITER for consistency with _write_file. Splitting by "§"
        # alone would incorrectly split entries that contain "§" in their content.
        entries = [e.strip() for e in raw.split(ENTRY_DELIMITER)]
        return [e for e in entries if e]

    @staticmethod
    def _write_file(path: Path, entries: List[str]):
        """Write entries to a memory file using atomic temp-file + rename.

        Previous implementation used open("w") + flock, but "w" truncates the
        file *before* the lock is acquired, creating a race window where
        concurrent readers see an empty file. Atomic rename avoids this:
        readers always see either the old complete file or the new one.
        """
        content = ENTRY_DELIMITER.join(entries) if entries else ""
        try:
            # Write to temp file in same directory (same filesystem for atomic rename)
            fd, tmp_path = tempfile.mkstemp(
                dir=str(path.parent), suffix=".tmp", prefix=".mem_"
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(content)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp_path, str(path))  # Atomic on same filesystem
            except BaseException:
                # Clean up temp file on any failure
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
        except (OSError, IOError) as e:
            raise RuntimeError(f"Failed to write memory file {path}: {e}")


def memory_tool(
    action: str,
    target: str = "memory",
    content: str = None,
    old_text: str = None,
    store: Optional[MemoryStore] = None,
) -> str:
    """
    Single entry point for the memory tool. Dispatches to MemoryStore methods.

    Returns JSON string with results.
    """
    if store is None:
        return tool_error("Memory is not available. It may be disabled in config or this environment.", success=False)

    if target not in ("memory", "user"):
        return tool_error(f"Invalid target '{target}'. Use 'memory' or 'user'.", success=False)

    if action == "add":
        if not content:
            return tool_error("Content is required for 'add' action.", success=False)
        result = store.add(target, content)

    elif action == "replace":
        if not old_text:
            return tool_error("old_text is required for 'replace' action.", success=False)
        if not content:
            return tool_error("content is required for 'replace' action.", success=False)
        result = store.replace(target, old_text, content)

    elif action == "remove":
        if not old_text:
            return tool_error("old_text is required for 'remove' action.", success=False)
        result = store.remove(target, old_text)

    else:
        return tool_error(f"Unknown action '{action}'. Use: add, replace, remove", success=False)

    return json.dumps(result, ensure_ascii=False)


def check_memory_requirements() -> bool:
    """Memory tool has no external requirements -- always available."""
    return True


# =============================================================================
# OpenAI Function-Calling Schema
# =============================================================================

MEMORY_SCHEMA = {
    "name": "memory",
    "description": (
        "Save durable information to persistent memory that survives across sessions. "
        "Memory is injected into future turns, so keep it compact and focused on facts "
        "that will still matter later.\n\n"
        "WHEN TO SAVE (do this proactively, don't wait to be asked):\n"
        "- User corrects you or says 'remember this' / 'don't do that again'\n"
        "- User shares a preference, habit, or personal detail (name, role, timezone, coding style)\n"
        "- You discover something about the environment (OS, installed tools, project structure)\n"
        "- You learn a convention, API quirk, or workflow specific to this user's setup\n"
        "- You identify a stable fact that will be useful again in future sessions\n\n"
        "PRIORITY: User preferences and corrections > environment facts > procedural knowledge. "
        "The most valuable memory prevents the user from having to repeat themselves.\n\n"
        "Do NOT save task progress, session outcomes, completed-work logs, or temporary TODO "
        "state to memory; use session_search to recall those from past transcripts.\n"
        "If you've discovered a new way to do something, solved a problem that could be "
        "necessary later, save it as a skill with the skill tool.\n\n"
        "TWO TARGETS:\n"
        "- 'user': who the user is -- name, role, preferences, communication style, pet peeves\n"
        "- 'memory': your notes -- environment facts, project conventions, tool quirks, lessons learned\n\n"
        "ACTIONS: add (new entry), replace (update existing -- old_text identifies it), "
        "remove (delete -- old_text identifies it).\n\n"
        "SKIP: trivial/obvious info, things easily re-discovered, raw data dumps, and temporary task state."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["add", "replace", "remove"],
                "description": "The action to perform."
            },
            "target": {
                "type": "string",
                "enum": ["memory", "user"],
                "description": "Which memory store: 'memory' for personal notes, 'user' for user profile."
            },
            "content": {
                "type": "string",
                "description": "The entry content. Required for 'add' and 'replace'."
            },
            "old_text": {
                "type": "string",
                "description": "Short unique substring identifying the entry to replace or remove."
            },
        },
        "required": ["action", "target"],
    },
}


# --- Registry ---
from tools.registry import registry, tool_error

registry.register(
    name="memory",
    toolset="memory",
    schema=MEMORY_SCHEMA,
    handler=lambda args, **kw: memory_tool(
        action=args.get("action", ""),
        target=args.get("target", "memory"),
        content=args.get("content"),
        old_text=args.get("old_text"),
        store=kw.get("store")),
    check_fn=check_memory_requirements,
    emoji="🧠",
)




