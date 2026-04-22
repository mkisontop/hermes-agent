#!/usr/bin/env python3
"""Hermes Claude Code Router.

Minimum viable routing layer before touching Hermes Agent internals.

Responsibilities:
1. receive user text
2. classify planning/coding/review/normal
3. build a compact context envelope
4. invoke ~/.hermes/tools/claude_code_delegate.py with the correct profile
5. validate the returned control-plan JSON shape
6. return a structured routing decision for Hermes

Current policy (Commit 3):
- planning -> delegate plan_minimal
- coding   -> delegate plan_minimal first (never patch directly here)
- review   -> delegate review
- normal   -> HERMES_NATIVE

This script intentionally does NOT patch Hermes core routing yet. It is a
standalone boundary tool to prove the routing flow is reliable before Commit 4.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

DELEGATE = Path.home() / ".hermes/tools/claude_code_delegate.py"
SCHEMA_PATH = Path.home() / ".hermes/schemas/claude_control_plan.schema.json"

PLANNING_TERMS = [
    "plan", "design", "architect", "roadmap", "strategy",
    "spec", "implementation plan", "break this down",
    "how should we build", "best way to build",
]

CODING_TERMS = [
    "implement", "fix", "patch", "refactor", "write code",
    "add tests", "debug", "make this pass", "modify files",
    "change the repo",
]

REVIEW_TERMS = [
    "review", "audit", "inspect diff", "find bugs",
    "security review", "safety review",
]


def route(message: str) -> str:
    lower = (message or "").lower()

    if any(term in lower for term in REVIEW_TERMS):
        return "review"

    if any(term in lower for term in CODING_TERMS):
        return "coding"

    if any(term in lower for term in PLANNING_TERMS):
        return "planning"

    return "normal"


def build_envelope(message: str, route_name: str, session_summary: str = "") -> str:
    """Build a compact context envelope for Claude Code.

    Keep this small. Hermes owns full memory/skills/tools; Claude Code gets
    only enough context to do the planning/review task well.
    """
    envelope = {
        "schema_version": 1,
        "route": route_name,
        "user_message": message,
        "session_summary": session_summary or "",
        "relevant_memories": [
            {"summary": "User prefers Claude Code Opus 4.7 max effort for planning/coding when available."}
        ],
        "skill_candidates": [
            {
                "name": "claude-code-controller",
                "description": "Route planning/coding/review through Claude Code with safety policy.",
                "risk": "medium",
            },
            {
                "name": "claude-code-opus-max",
                "description": "Delegate high-stakes planning/coding/review to Claude Code Opus 4.7.",
                "risk": "medium",
            },
        ],
        "policy": {
            "coding_requires_worktree": True,
            "dangerous_actions_require_confirmation": True,
            "critical_actions_manual_only": True,
            "auto_merge_enabled": False,
        },
    }

    # Human-facing instruction around the JSON envelope.
    return (
        "You are Claude Code acting as a delegated planner/reviewer for Hermes. "
        "Respect the provided route and policy. Return the final answer directly; "
        "do not write files unless the profile explicitly allows edits.\n\n"
        f"{json.dumps(envelope, indent=2)}"
    )


def _extract_delegate_result_text(raw_output: str) -> str:
    """Extract the useful assistant result from delegate stream-json output.

    Preference order:
      1. final {type: result}.result
      2. concatenated assistant text_delta stream chunks
      3. raw output fallback
    """
    lines = [ln for ln in raw_output.splitlines() if ln.strip()]
    text_chunks: list[str] = []
    final_result: Optional[str] = None

    for line in lines:
        try:
            obj = json.loads(line)
        except Exception:
            continue

        if obj.get("type") == "result":
            result_val = obj.get("result")
            if isinstance(result_val, str) and result_val.strip():
                final_result = result_val

        if obj.get("type") == "stream_event":
            event = obj.get("event") or {}
            delta = event.get("delta") or {}
            if delta.get("type") == "text_delta":
                txt = delta.get("text")
                if isinstance(txt, str):
                    text_chunks.append(txt)

    if final_result:
        return final_result.strip()
    if text_chunks:
        return "".join(text_chunks).strip()
    return raw_output.strip()


def call_delegate(profile: str, prompt: str, cwd: str, timeout: int = 3600) -> dict[str, Any]:
    """Invoke the Claude Code delegate and return structured metadata."""
    if not DELEGATE.exists():
        raise FileNotFoundError(f"Delegate not found: {DELEGATE}")

    cmd = [str(DELEGATE), profile, prompt, "--cwd", cwd, "--timeout", str(timeout)]
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    raw = proc.stdout or ""
    extracted = _extract_delegate_result_text(raw)

    # Find the final metadata object emitted by the delegate (run_id / profile / exit_code / log_path).
    metadata: dict[str, Any] = {}
    for line in reversed(raw.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if all(k in obj for k in ("run_id", "profile", "exit_code", "log_path")):
            metadata = obj
            break

    return {
        "subprocess_exit_code": proc.returncode,
        "delegate_metadata": metadata,
        "delegate_raw_output": raw,
        "delegate_result_text": extracted,
        "delegate_profile": profile,
    }


def _validate_control_plan(obj: dict[str, Any]) -> None:
    """Minimal schema validation without extra dependencies."""
    required = [
        "schema_version",
        "route",
        "decision",
        "confidence",
        "skills_to_use",
        "next_profile",
        "requires_user_confirmation",
        "response",
    ]
    allowed = set(required) | {"reasoning_summary", "confirmation_prompt"}
    missing = [k for k in required if k not in obj]
    if missing:
        raise ValueError(f"control plan missing required keys: {missing}")
    extra = sorted(set(obj.keys()) - allowed)
    if extra:
        raise ValueError(f"control plan has unexpected keys: {extra}")
    if obj["schema_version"] != 1:
        raise ValueError("schema_version must be 1")
    if obj["route"] not in {"planning", "coding", "review", "normal"}:
        raise ValueError(f"invalid route: {obj['route']}")
    if obj["decision"] not in {
        "answer", "ask_clarifying", "use_skill", "delegate_plan",
        "delegate_patch", "delegate_review", "refuse",
    }:
        raise ValueError(f"invalid decision: {obj['decision']}")
    if obj["next_profile"] not in {"none", "plan_minimal", "plan_files", "patch_worktree", "review"}:
        raise ValueError(f"invalid next_profile: {obj['next_profile']}")
    conf = obj["confidence"]
    if not isinstance(conf, (int, float)) or conf < 0 or conf > 1:
        raise ValueError(f"invalid confidence: {conf}")
    if not isinstance(obj["skills_to_use"], list):
        raise ValueError("skills_to_use must be a list")
    if not isinstance(obj["requires_user_confirmation"], bool):
        raise ValueError("requires_user_confirmation must be boolean")


def _json_safe_text(text: Optional[str]) -> Optional[str]:
    """Normalize delegate text so router output is always valid JSON.

    Stream-json runs can include stray control chars or invalid surrogate
    sequences. Hermes routing output should be boring, schema-clean JSON.
    """
    if text is None:
        return None
    # Drop C0 controls except common whitespace.
    cleaned = []
    for ch in str(text):
        o = ord(ch)
        if ch in "\n\r\t" or o >= 32:
            cleaned.append(ch)
    return "".join(cleaned)


def make_control_plan(message: str, cwd: str, session_summary: str = "", timeout: int = 3600) -> dict[str, Any]:
    route_name = route(message)

    if route_name == "normal":
        plan = {
            "schema_version": 1,
            "route": "normal",
            "decision": "answer",
            "confidence": 0.9,
            "reasoning_summary": "No planning/coding/review trigger terms detected; keep this request Hermes-native.",
            "skills_to_use": [],
            "next_profile": "none",
            "requires_user_confirmation": False,
            "confirmation_prompt": None,
            "response": "HERMES_NATIVE",
        }
        _validate_control_plan(plan)
        return plan

    if route_name == "planning":
        delegate = call_delegate(
            "plan_minimal",
            build_envelope(message, route_name, session_summary),
            cwd,
            timeout=timeout,
        )
        plan = {
            "schema_version": 1,
            "route": "planning",
            "decision": "delegate_plan",
            "confidence": 0.85,
            "reasoning_summary": "Planning/design/architecture language detected; route to Claude Code plan_minimal.",
            "skills_to_use": ["claude-code-controller", "claude-code-opus-max"],
            "next_profile": "plan_minimal",
            "requires_user_confirmation": False,
            "confirmation_prompt": None,
            "response": _json_safe_text(delegate["delegate_result_text"]),
        }
        _validate_control_plan(plan)
        return plan

    if route_name == "coding":
        delegate = call_delegate(
            "plan_minimal",
            build_envelope(message, route_name, session_summary)
            + "\n\nThis is a coding request. Do NOT edit files. Produce the implementation plan first."
            + " The next step after acceptance would be patch_worktree.",
            cwd,
            timeout=timeout,
        )
        plan = {
            "schema_version": 1,
            "route": "coding",
            "decision": "delegate_plan",
            "confidence": 0.9,
            "reasoning_summary": "Coding/debug/refactor language detected; require plan-first flow before any patching.",
            "skills_to_use": ["claude-code-controller", "claude-code-opus-max"],
            "next_profile": "patch_worktree",
            "requires_user_confirmation": True,
            "confirmation_prompt": "Claude Code produced a plan. Review/approve it before patch_worktree.",
            "response": _json_safe_text(delegate["delegate_result_text"]),
        }
        _validate_control_plan(plan)
        return plan

    if route_name == "review":
        delegate = call_delegate(
            "review",
            build_envelope(message, route_name, session_summary),
            cwd,
            timeout=timeout,
        )
        plan = {
            "schema_version": 1,
            "route": "review",
            "decision": "delegate_review",
            "confidence": 0.9,
            "reasoning_summary": "Review/audit language detected; route to Claude Code review profile.",
            "skills_to_use": ["claude-code-controller", "claude-code-opus-max"],
            "next_profile": "review",
            "requires_user_confirmation": False,
            "confirmation_prompt": None,
            "response": _json_safe_text(delegate["delegate_result_text"]),
        }
        _validate_control_plan(plan)
        return plan

    raise RuntimeError(f"unhandled route: {route_name}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--message", required=True, help="User message to route")
    ap.add_argument("--cwd", default=os.getcwd(), help="Working directory for delegate calls")
    ap.add_argument("--session-summary", default="", help="Optional compact session summary")
    ap.add_argument("--timeout", type=int, default=3600, help="Delegate timeout seconds")
    ns = ap.parse_args()

    try:
        plan = make_control_plan(ns.message, ns.cwd, ns.session_summary, ns.timeout)
    except Exception as e:
        err = {
            "schema_version": 1,
            "route": route(ns.message),
            "decision": "refuse",
            "confidence": 0.2,
            "reasoning_summary": f"Router failed: {type(e).__name__}: {e}",
            "skills_to_use": ["claude-code-controller"],
            "next_profile": "none",
            "requires_user_confirmation": False,
            "confirmation_prompt": None,
            "response": None,
        }
        print(json.dumps(err, indent=2))
        return 1

    print(json.dumps(plan, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
