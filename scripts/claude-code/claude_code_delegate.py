#!/usr/bin/env python3
"""
Hermes Claude Code Delegate

Purpose:
  - Invoke official Claude Code CLI through ~/.hermes/bin/hermes-claude-opus-max
  - Preserve Claude Max subscription OAuth path
  - Prevent API-key auth leakage
  - Enforce bounded profiles: smoke, plan_minimal, plan_files,
    patch_worktree, review
  - Capture JSON/stream-json logs
  - Return structured metadata to Hermes

Commit 1 policy:
  - plan_minimal is the default Hermes planning profile
  - plan_minimal is intentionally almost non-agentic: Read-only, no Bash,
    no Grep/Glob, no MCP, no slash commands, no session persistence
  - plan_files is the explicit-file-context planning profile; still Read-only
  - patch_worktree is the only implementation profile; worktree is mandatory
  - review is read-only scrutiny after changes
  - --bare is forbidden for this Max-subscription integration because it skips
    OAuth/keychain reads and pushes authentication toward API-key paths
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

WRAPPER = Path.home() / ".hermes/bin/hermes-claude-opus-max"
LOG_ROOT = Path.home() / ".hermes/logs/claude-code-runs"
MINIMAL_SETTINGS = Path(
    os.environ.get(
        "HERMES_CLAUDE_MINIMAL_SETTINGS",
        str(Path.home() / ".hermes/claude-code/minimal-settings.json"),
    )
)
EMPTY_MCP_CONFIG = '{"mcpServers":{}}'


def clean_env() -> dict[str, str]:
    """Return an environment with API-billing vars scrubbed."""
    env = os.environ.copy()
    for key in [
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_MODEL",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL",
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_VERTEX",
        "CLAUDE_CODE_USE_FOUNDRY",
    ]:
        env.pop(key, None)
    env.setdefault("CLAUDE_CODE_EFFORT_LEVEL", "max")
    env.setdefault("CLAUDE_CODE_MAX_RETRIES", "1")
    env.setdefault("CLAUDE_CODE_MAX_TOOL_USE_CONCURRENCY", "4")
    env.setdefault("CLAUDE_CODE_MAX_OUTPUT_TOKENS", "16384")
    return env


def base_cmd(model: str, effort: str) -> list[str]:
    return [
        str(WRAPPER),
        "-p",
        "--model", model,
        "--effort", effort,
        "--no-session-persistence",
    ]


def _append_stream_json(cmd: list[str]) -> None:
    cmd += [
        "--output-format", "stream-json",
        "--verbose",
        "--include-partial-messages",
    ]


def build_cmd(args: argparse.Namespace) -> list[str]:
    cmd = base_cmd(args.model, args.effort)

    if args.profile == "smoke":
        cmd += [
            "--tools", "",
            "--permission-mode", "plan",
            "--max-turns", "1",
            "--output-format", "json",
            args.prompt,
        ]

    elif args.profile == "plan_minimal":
        cmd += [
            "--permission-mode", "plan",
            "--tools", "Read",
            "--allowedTools", "Read",
            "--disallowedTools",
            "Bash",
            "Edit",
            "Write",
            "WebFetch",
            "Grep",
            "Glob",
            "--disable-slash-commands",
            "--setting-sources", "project",
            "--strict-mcp-config",
            "--mcp-config", EMPTY_MCP_CONFIG,
            "--max-turns", str(args.max_turns or 6),
        ]
        if MINIMAL_SETTINGS.exists():
            cmd += ["--settings", str(MINIMAL_SETTINGS)]
        _append_stream_json(cmd)
        cmd.append(args.prompt)

    elif args.profile == "plan_files":
        cmd += [
            "--permission-mode", "plan",
            "--tools", "Read",
            "--allowedTools", "Read",
            "--disallowedTools",
            "Bash",
            "Edit",
            "Write",
            "WebFetch",
            "Grep",
            "Glob",
            "--disable-slash-commands",
            "--setting-sources", "project",
            "--strict-mcp-config",
            "--mcp-config", EMPTY_MCP_CONFIG,
            "--max-turns", str(args.max_turns or 6),
        ]
        if MINIMAL_SETTINGS.exists():
            cmd += ["--settings", str(MINIMAL_SETTINGS)]
        _append_stream_json(cmd)
        cmd.append(args.prompt)

    elif args.profile in {"patch_worktree", "patch"}:
        if not args.worktree:
            raise SystemExit("--worktree is required for patch_worktree profile")
        cmd += [
            "--worktree", args.worktree,
            "--permission-mode", "acceptEdits",
            "--tools", "Read,Grep,Glob,Edit,Write,Bash",
            "--allowedTools",
            "Read",
            "Grep",
            "Glob",
            "Edit",
            "Write",
            "Bash(git status)",
            "Bash(git diff *)",
            "Bash(git log *)",
            "Bash(pytest -q)",
            "Bash(./smoke_test.sh t1)",
            "Bash(./smoke_test.sh t5)",
            "Bash(python -m evolution.doctor_config)",
            "Bash(python -m evolution.doctor_config --live)",
            "--disallowedTools",
            "Bash(git push *)",
            "Bash(rm -rf *)",
            "Bash(sudo *)",
            "Bash(curl * | sh *)",
            "Bash(wget * | sh *)",
            "--max-turns", str(args.max_turns or 24),
        ]
        _append_stream_json(cmd)
        cmd.append(args.prompt)

    elif args.profile in {"review"}:
        cmd += [
            "--permission-mode", "plan",
            "--tools", "Read,Grep,Glob,Bash",
            "--allowedTools",
            "Read",
            "Grep",
            "Glob",
            "Bash(git status)",
            "Bash(git diff *)",
            "Bash(git log *)",
            "Bash(pytest -q)",
            "--disallowedTools",
            "Edit",
            "Write",
            "Bash(git push *)",
            "Bash(rm -rf *)",
            "Bash(sudo *)",
            "--max-turns", str(args.max_turns or 10),
        ]
        _append_stream_json(cmd)
        cmd.append(args.prompt)

    elif args.profile in {"plan"}:
        # Compatibility alias. Hermes automation should prefer plan_minimal.
        return build_cmd(
            argparse.Namespace(
                profile="plan_minimal",
                prompt=args.prompt,
                cwd=args.cwd,
                worktree=args.worktree,
                model=args.model,
                effort=args.effort,
                max_turns=args.max_turns,
                timeout=args.timeout,
            )
        )

    else:
        raise SystemExit(f"unknown profile: {args.profile}")

    return cmd


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "profile",
        choices=["smoke", "plan", "plan_minimal", "plan_files", "patch", "patch_worktree", "review"],
    )
    parser.add_argument("prompt")
    parser.add_argument("--cwd", default=os.getcwd())
    parser.add_argument("--worktree")
    parser.add_argument("--model", default="claude-opus-4-7")
    parser.add_argument("--effort", default="max")
    parser.add_argument("--max-turns", type=int)
    parser.add_argument("--timeout", type=int, default=3600)
    ns = parser.parse_args()

    if not WRAPPER.exists():
        raise SystemExit(f"missing wrapper: {WRAPPER}")

    LOG_ROOT.mkdir(parents=True, exist_ok=True)
    run_id = time.strftime("%Y%m%d_%H%M%S")
    log_path = LOG_ROOT / f"{run_id}_{ns.profile}.log"

    cmd = build_cmd(ns)
    env = clean_env()

    started = time.time()
    child_started = False
    child_rc: int | None = None
    signal_name: str | None = None

    with log_path.open("w", encoding="utf-8") as log:
        log.write(json.dumps({
            "run_id": run_id,
            "profile": ns.profile,
            "cwd": ns.cwd,
            "model": ns.model,
            "effort": ns.effort,
            "worktree": ns.worktree,
            "minimal_settings": str(MINIMAL_SETTINGS),
            "mcp_config": EMPTY_MCP_CONFIG,
            "cmd": cmd,
        }, indent=2))
        log.write("\n\n")

        def _log_marker(marker: str, **data) -> None:
            payload = {"marker": marker, **data}
            line = json.dumps(payload, sort_keys=True)
            print(line)
            log.write(line + "\n")
            log.flush()

        proc = subprocess.Popen(
            cmd,
            cwd=ns.cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        child_started = True
        _log_marker("child_started", pid=proc.pid)

        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                print(line, end="")
                log.write(line)
            child_rc = proc.wait(timeout=ns.timeout)
            rc = child_rc
        except subprocess.TimeoutExpired:
            _log_marker("delegate_signal", signal="timeout", timeout=ns.timeout)
            proc.kill()
            child_rc = 124
            rc = 124
        except KeyboardInterrupt:
            signal_name = "SIGINT"
            _log_marker("delegate_signal", signal=signal_name)
            proc.kill()
            child_rc = 130
            rc = 130
        finally:
            if child_started:
                try:
                    polled = proc.poll()
                except Exception:
                    polled = None
                if polled is not None:
                    child_rc = polled
                _log_marker("child_exit", returncode=child_rc)

    elapsed = round(time.time() - started, 2)
    print(json.dumps({
        "run_id": run_id,
        "profile": ns.profile,
        "exit_code": rc,
        "elapsed_s": elapsed,
        "log_path": str(log_path),
    }, indent=2))
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
