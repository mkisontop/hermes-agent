---
name: claude-code-controller
description: Route planning, architecture, coding, debugging, and code-review work through Claude Code Opus 4.7 while Hermes keeps memory, skills, tools, confirmations, and safety.
version: 1.0.0
metadata:
  hermes:
    risk: medium
    tags: [claude-code, router, planning, coding, review, opus, max-effort]
    related_skills: [claude-code-opus-max, claude-code]
---

# Claude Code Controller

Use this skill for **every planning, design, architecture, implementation, debugging, refactoring, test-writing, or code-review request**.

Hermes must not answer planning or coding tasks directly when Claude Code is available.

## Core rule

When the user asks for work that is fundamentally about:
- planning
- design
- architecture
- implementation
- coding
- debugging
- refactoring
- test writing
- code review
- safety review

Hermes should route the request to Claude Code using the correct delegate profile.

GPT-5.4 may still:
- classify intent
- load small relevant memory/context
- enforce policy
- ask for confirmations when needed
- summarize or present Claude Code output

But GPT-5.4 should **not** be the final planning/coding brain when Claude Code is available.

## Delegate profiles

Always invoke through:

```bash
~/.hermes/tools/claude_code_delegate.py
```

Canonical profiles:
- `smoke`
- `plan_minimal`
- `plan_files`
- `patch_worktree`
- `review`

### Planning
Use `plan_minimal` for:
- architecture
- design
- roadmap/spec
- implementation plan
- task breakdown
- “how should we build this?”

Use `plan_files` only when planning genuinely needs explicit repo context.

### Coding
For coding requests, the required flow is:

```text
coding request → plan_minimal first → user confirmation if medium/high risk → patch_worktree
```

Do **not** jump straight to `patch_worktree` for vague requests.

Examples that should route to `plan_minimal` first:
- “help me improve Hermes”
- “refactor this system”
- “make this architecture better”
- “debug this generally”

Examples that may proceed to `patch_worktree` after plan acceptance:
- “implement Batch C according to this accepted plan”
- “fix this failing test in a worktree”
- “apply this specific refactor and preserve tests”

### Review
Use `review` for:
- code review
- safety review
- security review
- diff audit
- bug hunt on an existing patch

## Intent routing policy

### Route to Claude Code for:
- all planning
- all architecture
- all coding
- all debugging
- all repo review
- all safety/security review
- hard refactors
- failing test diagnosis
- expensive-to-get-wrong code changes

### Keep Hermes-native for:
- normal chat
- memory recall
- messaging / calendar / email
- simple command routing
- skill discovery
- routine automation
- nightly self-evolution orchestration
- proposal lifecycle UI

If a “normal” task becomes planning/coding during the conversation, escalate to Claude Code.

## Safety rules

Never:
- call raw `claude` from Hermes automation
- use `--bare`
- use `claude auth login --console`
- rely on `ANTHROPIC_API_KEY` / `ANTHROPIC_AUTH_TOKEN` for this path
- use `--dangerously-skip-permissions`
- use unrestricted Bash
- bypass `proposal_reviewer`
- bypass `write_back_skill`
- auto-approve critical or destructive actions
- silently patch the repo with GPT-5.4 when Claude Code delegation fails

If Claude Code fails:
- planning request → report the failure and offer fallback
- coding request → do **not** silently patch with Hermes-native reasoning
- review request → do **not** silently downgrade to GPT-5.4 safety review

## Profile guidance

### `plan_minimal`
Default planning mode.

Properties:
- Read-only
- no Bash
- no Grep/Glob
- no MCP
- no slash commands
- no session persistence
- constrained turns

Use this first.

### `plan_files`
Use only when the prompt explicitly provides a file list or repo context is necessary.

Properties:
- Read-only
- explicit file list only
- no Bash
- no broad repo touring

### `patch_worktree`
The only implementation profile.

Properties:
- worktree required
- bounded Bash only for tests and git inspection
- never writes directly to main
- always followed by tests and review

### `review`
Read-only scrutiny after changes.

Properties:
- no edits
- focus on bugs, safety regressions, stale-hash bypasses, manifest weakening, path escapes, auto-merge footguns, missing tests

## Required workflow after patch mode

After `patch_worktree`, Hermes must run or instruct the operator to run:

```bash
git status --short
git diff --stat
pytest -q
./smoke_test.sh t1
./smoke_test.sh t5
python -m evolution.doctor_config
python -m evolution.doctor_config --live
python -m evolution.doctor_config --judge-canary writing-plans
```

Then run `review` before committing.

## Completion contract

After every Claude Code run, Hermes must report:
- profile used
- model and effort
- whether Max/subscription auth path was assumed/checked
- worktree name, if any
- files changed (if any)
- tests run
- exit code
- recommended next action

## Current self-evolution safety context

For the Hermes self-evolution repo specifically:
- Batch A hard-block on `hermes-self-evolution` must remain intact
- Batch A-prime judge-phase containment must remain intact
- Batch B manifest / stale-baseline / atomic-write safety must remain intact
- real auto-merge remains disabled by default
- GEPA is **not** the default optimizer
- `codex-spark` is optimizer/proposer/task; `gpt-5.4` is judge/eval only

## How Hermes should use this skill now

Current staged rollout:
1. use this skill to route planning requests to `plan_minimal`
2. once stable, route explicit file-aware planning to `plan_files`
3. only after plan acceptance, route implementation to `patch_worktree`
4. always route post-change audits to `review`
5. hard Hermes routing now exists behind environment toggles in the gateway

### Hard-routing state (Commit 4)

Hermes now has a pre-reasoning gateway hook that can route free-text
planning/coding/review requests through `~/.hermes/tools/hermes_claude_router.py`
*before* normal GPT-5.4 reasoning — but only when explicitly enabled.

Master toggle:

```bash
export HERMES_CLAUDE_CODE_CONTROL=1
```

Per-route toggles:

```bash
export HERMES_CLAUDE_CODE_PLAN=1
export HERMES_CLAUDE_CODE_CODING=1
export HERMES_CLAUDE_CODE_REVIEW=1
```

Safety semantics of the hard router:
- slash commands do **not** go through Claude routing
- planning failures return explicit failure text (no fake success)
- coding failures do **not** silently patch with GPT-5.4
- review failures do **not** silently downgrade safety review to GPT-5.4
- `coding` still means **plan first**, with `requires_user_confirmation=true` before `patch_worktree`

The router script and schema currently live at:

```bash
~/.hermes/tools/hermes_claude_router.py
~/.hermes/schemas/claude_control_plan.schema.json
```

This skill remains the prompt-level controller and policy layer. The hard
router now exists, but stays opt-in behind env toggles so operators can turn
it off instantly if Claude delegation degrades.