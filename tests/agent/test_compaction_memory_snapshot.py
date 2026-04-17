"""Tests for Fix #2: memory snapshot injected into compaction prompts.

Fix #2 ensures durable memory (USER + MEMORY entries) survives the
compaction boundary — the summarizer LLM sees the live memory snapshot
in the prompt and is instructed to preserve it verbatim in the output.

Scope: `_build_memory_snapshot_block()` helper + integration into
`_generate_summary()` prompt construction.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agent.context_compressor import (
    ContextCompressor,
    SUMMARY_PREFIX,
    _build_memory_snapshot_block,
)


@pytest.fixture
def memory_dir(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with seeded MEMORY.md and USER.md."""
    home = tmp_path / "hermes_home"
    mem = home / "memories"
    mem.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    return mem


@pytest.fixture
def compressor():
    with patch("agent.context_compressor.get_model_context_length", return_value=100000):
        return ContextCompressor(
            model="test/model",
            threshold_percent=0.85,
            protect_first_n=2,
            protect_last_n=2,
            quiet_mode=True,
        )


# ---------------------------------------------------------------------------
# _build_memory_snapshot_block — pure helper
# ---------------------------------------------------------------------------


class TestBuildMemorySnapshotBlock:
    def test_returns_empty_when_no_memory_files(self, memory_dir):
        """No MEMORY.md or USER.md → return empty string (no block)."""
        block = _build_memory_snapshot_block()
        assert block == ""

    def test_includes_user_entries(self, memory_dir):
        (memory_dir / "USER.md").write_text(
            "Address the user as MK.\n§\nUser prefers blunt feedback.\n"
        )
        block = _build_memory_snapshot_block()
        assert "Address the user as MK." in block
        assert "User prefers blunt feedback." in block

    def test_includes_memory_entries(self, memory_dir):
        (memory_dir / "MEMORY.md").write_text(
            "Local gateway at http://localhost:20128/v1.\n§\n"
            "Asia/Tehran timezone for all crons.\n"
        )
        block = _build_memory_snapshot_block()
        assert "Local gateway" in block
        assert "Asia/Tehran" in block

    def test_has_preservation_header(self, memory_dir):
        """Block MUST tell the summarizer to preserve verbatim."""
        (memory_dir / "USER.md").write_text("Call the user MK.\n")
        block = _build_memory_snapshot_block()
        # Any language that signals "preserve / copy verbatim / do not summarize"
        assert any(
            kw in block.lower()
            for kw in ("preserve", "verbatim", "do not summarize", "copy exactly")
        ), f"block lacks preservation directive:\n{block}"

    def test_labels_user_and_memory_sections_separately(self, memory_dir):
        (memory_dir / "USER.md").write_text("u1\n§\nu2\n")
        (memory_dir / "MEMORY.md").write_text("m1\n§\nm2\n")
        block = _build_memory_snapshot_block()
        # Must distinguish USER from MEMORY so the summarizer keeps them separate.
        lower = block.lower()
        assert "user" in lower and "memory" in lower

    def test_failure_safe_on_corrupt_files(self, memory_dir):
        """Garbage in memory files must not crash summarization."""
        (memory_dir / "USER.md").write_bytes(b"\xff\xfe\x00\x00garbage")
        (memory_dir / "MEMORY.md").write_text("valid entry\n")
        block = _build_memory_snapshot_block()
        # Either returns clean partial block or empty string — never raises.
        assert isinstance(block, str)

    def test_respects_entry_budget(self, memory_dir):
        """Snapshot must not explode the prompt — budget caps entries."""
        entries = "\n§\n".join(f"entry {i}" * 50 for i in range(200))
        (memory_dir / "MEMORY.md").write_text(entries)
        block = _build_memory_snapshot_block()
        # Hard ceiling so a bloated MEMORY.md can't blow the prompt budget.
        assert len(block) < 8000, f"block too large: {len(block)} chars"


# ---------------------------------------------------------------------------
# Integration: _generate_summary injects the block into the prompt
# ---------------------------------------------------------------------------


class TestGenerateSummaryInjectsMemory:
    def _fake_llm_response(self, text: str = "## Active Task\nnone"):
        r = MagicMock()
        r.choices = [MagicMock()]
        r.choices[0].message.content = text
        return r

    def test_memory_block_reaches_llm_prompt(self, memory_dir, compressor):
        (memory_dir / "USER.md").write_text("Call the user MK.\n")
        (memory_dir / "MEMORY.md").write_text("Gateway at localhost:20128.\n")

        captured = {}

        def fake_call(*args, **kwargs):
            # call_llm signature varies; snapshot the messages list.
            messages = kwargs.get("messages") or (args[1] if len(args) > 1 else None)
            captured["messages"] = messages
            return self._fake_llm_response()

        msgs = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ] * 5

        with patch("agent.context_compressor.call_llm", side_effect=fake_call):
            compressor._generate_summary(msgs)

        # Flatten the prompt text the LLM actually saw.
        prompt_text = ""
        for m in (captured.get("messages") or []):
            c = m.get("content") or ""
            if isinstance(c, str):
                prompt_text += c

        assert "Call the user MK." in prompt_text, "USER memory missing from prompt"
        assert "Gateway at localhost:20128." in prompt_text, "MEMORY missing from prompt"

    def test_empty_memory_does_not_break_summary(self, memory_dir, compressor):
        """No memory files → summary still works."""
        msgs = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ] * 5

        with patch(
            "agent.context_compressor.call_llm",
            return_value=self._fake_llm_response(),
        ):
            out = compressor._generate_summary(msgs)

        assert isinstance(out, str)
        assert out.startswith(SUMMARY_PREFIX)
