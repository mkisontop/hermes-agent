# Hermes + Claude Code Hybrid System

This document explains the **hybrid Claude Code + Hermes system** that was built to let Hermes keep its strengths (memory, tools, gateways, cron, policy, proposal lifecycle) while delegating hard planning/coding/review work to **Claude Code Opus 4.7**.

## The core idea

```text
User -> Hermes Agent (router / policy / context)
      -> Hermes-native path for normal chat, memory, tools, automation
      -> Claude Code path for planning, design, coding, debugging, review
```

Hermes remains the orchestrator.
Claude Code becomes the high-intelligence planning/coding/review delegate.

## Why this exists

Hermes is best at:
- persistent memory
- skills
- tools and tool orchestration
- messaging gateways
- cron / automation
- policy and approval flows
- proposal lifecycle and safe write-back

Claude Code is best at:
- repo-aware planning
- architecture
- coding
- debugging
- code review
- deep reasoning on multi-file changes

The result is a clean split:

```text
Hermes = memory, tools, policy, execution, gateways
Claude Code = planning, coding, review brain
GPT-5.4 = thin router / summarizer / policy layer
```

---

## Authentication model

This system is designed for **Claude Max subscription auth**, not API-key auth.

### Wrapper path

Use the wrapper:

- `scripts/claude-code/hermes-claude-opus-max`

It strips the env vars that would otherwise force API billing or alternate backends:
- `ANTHROPIC_API_KEY`
- `ANTHROPIC_AUTH_TOKEN`
- `ANTHROPIC_BASE_URL`
- `ANTHROPIC_MODEL`
- `ANTHROPIC_DEFAULT_HAIKU_MODEL`
- `CLAUDE_CODE_USE_BEDROCK`
- `CLAUDE_CODE_USE_VERTEX`
- `CLAUDE_CODE_USE_FOUNDRY`

### Never do this for the Max path
- `claude auth login --console`
- `--bare`
- use raw `ANTHROPIC_API_KEY` for delegated runs

### Quick auth verification

```bash
unset ANTHROPIC_API_KEY ANTHROPIC_AUTH_TOKEN ANTHROPIC_BASE_URL ANTHROPIC_MODEL ANTHROPIC_DEFAULT_HAIKU_MODEL
~/.hermes/bin/hermes-claude-opus-max auth status --text
```

Expected:

```text
Login method: Claude Max account
```

---

## Canonical delegate profiles

The delegate exposes these canonical profiles:

- `smoke`
- `plan_minimal`
- `plan_files`
- `patch_worktree`
- `review`

Compatibility aliases may exist:
- `plan` -> `plan_minimal`
- `patch` -> `patch_worktree`

### 1. `smoke`
Use to prove:
- wrapper works
- auth path works
- delegate works
- logging works

Example:

```bash
~/.hermes/tools/claude_code_delegate.py smoke \
  "Return exactly PONG." \
  --cwd ~/.hermes/self-evolution \
  --timeout 120
```

### 2. `plan_minimal`
Default planning mode.

Properties:
- Read-only
- no Bash
- no Grep/Glob
- no MCP
- no slash commands
- no session persistence
- constrained turns
- stream-json output

Use for:
- architecture
- design
- roadmap/spec
- implementation plan
- “how should we build this?”

### 3. `plan_files`
Planning with explicit file context.

Properties:
- Read-only
- explicit file list only
- no Bash
- no broad repo touring

Use only when planning genuinely needs repo files.

### 4. `patch_worktree`
Implementation mode.

Properties:
- worktree required
- bounded Bash only for tests / git inspection
- stream-json logs
- never patches main directly without review

### 5. `review`
Read-only review mode.

Properties:
- no edits
- used after patch mode
- focuses on safety regressions, stale-hash bypasses, path escapes, auto-merge footguns, missing tests

---

## Stable minimal settings

File:
- `scripts/claude-code/minimal-settings.json`

Purpose:
- suppress hook noise
- make planning almost non-agentic
- deny sensitive reads even in read-only runs

Typical installation path:
- `~/.hermes/claude-code/minimal-settings.json`

### Important MCP detail
For the installed Claude Code CLI, this is **invalid**:

```bash
--mcp-config '{}'
```

Use this instead:

```bash
--strict-mcp-config --mcp-config '{"mcpServers":{}}'
```

---

## Router pieces

### Control schema
File:
- `scripts/claude-code/claude_control_plan.schema.json`

Defines a structured routing/decision payload with fields like:
- `route`
- `decision`
- `confidence`
- `next_profile`
- `requires_user_confirmation`
- `response`

### Router script
File:
- `scripts/claude-code/hermes_claude_router.py`

Responsibilities:
1. receive user text
2. classify planning / coding / review / normal
3. build compact context envelope
4. invoke `claude_code_delegate.py`
5. normalize the result into schema-clean JSON
6. return a routing decision

Current behavior:
- planning -> delegate `plan_minimal`
- coding -> delegate `plan_minimal` first, never patch directly, confirmation required before `patch_worktree`
- review -> delegate `review`
- normal -> `HERMES_NATIVE`

---

## Controller skill

File:
- `skills/autonomous-ai-agents/claude-code-controller/SKILL.md`

Purpose:
- prompt-level routing policy
- tells Hermes to route planning/design/architecture/coding/debugging/refactoring/review to Claude Code
- keeps normal chat/memory/tooling Hermes-native
- enforces plan-first for coding

This is the **soft routing layer** before hard internal routing.

---

## Hard routing inside Hermes

The Hermes Agent repo now also contains a hard routing hook in the gateway.

Commit:
- `525599c3` — `Add env-gated Claude Code hard routing hook`

File touched:
- `gateway/run.py`

The hook runs:
- after slash-command handling
- before normal Hermes reasoning

### Env toggles
Master switch:

```bash
export HERMES_CLAUDE_CODE_CONTROL=1
```

Per-route switches:

```bash
export HERMES_CLAUDE_CODE_PLAN=1
export HERMES_CLAUDE_CODE_CODING=1
export HERMES_CLAUDE_CODE_REVIEW=1
```

### Hard-routing semantics
- planning free text -> Claude Code planning
- coding free text -> Claude Code plan-first, not direct patch
- review free text -> Claude Code review
- slash commands stay local
- normal messages stay Hermes-native

### Safety guarantees
If Claude Code routing fails:
- planning: explicit failure text
- coding: no silent GPT-5.4 repo patching
- review: no silent GPT-5.4 safety-review downgrade

---

## `/md` support for long Telegram pastes

The system also fixed long `/md` behavior.

### Problem
Telegram can split a long `/md ...` message into multiple chunks.
Before the fix:
- first chunk went through command handling immediately
- later chunks came as plain text
- only the first chunk was written to the markdown file

### Fixes
1. `/md` is now batched through the Telegram text batching path so continuation chunks can merge
2. plugin slash-command dispatch now lazily calls `discover_plugins()` before lookup, so `md-drop` is actually visible to the gateway

Plugin path:
- `~/.hermes/plugins/md-drop/`

Effect:
- long `/md` pastes can be saved to one `.md` file under `~/.hermes/inbox/md/`
- Hermes reads the file and responds to the contents as if sent directly

---

## Safety boundaries

### Hermes keeps control of
- memory
- skills
- messaging
- tool orchestration
- proposal lifecycle
- risk policy
- write-back safety

### Claude Code handles
- planning
- architecture
- coding
- debugging
- review

### Coding is always staged

```text
coding request
-> plan_minimal first
-> user confirmation
-> patch_worktree
-> tests / doctor
-> review
-> then commit / merge by user
```

This prevents Claude Code from becoming an uncontrolled patch bot.

---

## Recommended verification commands

### Smoke
```bash
~/.hermes/tools/claude_code_delegate.py smoke \
  "Return exactly PONG." \
  --cwd ~/.hermes/self-evolution \
  --timeout 120
```

### Minimal planning
```bash
~/.hermes/tools/claude_code_delegate.py plan_minimal \
  "Create a concise 5-step plan for adding a Hermes router that sends planning and coding tasks to Claude Code. Do not inspect files." \
  --cwd ~/.hermes/self-evolution \
  --timeout 600
```

### File-limited planning
```bash
cat > /tmp/hermes_plan_files_test.txt <<'EOF'
Read only these files:
1. CLAUDE.md
2. .claude/settings.json

Do not use Bash.
Do not search broadly.
Return a short summary of the Claude Code policy in this repo.
EOF

~/.hermes/tools/claude_code_delegate.py plan_files \
  "$(cat /tmp/hermes_plan_files_test.txt)" \
  --cwd ~/.hermes/self-evolution \
  --timeout 900
```

### Router smoke
```bash
~/.hermes/tools/hermes_claude_router.py --message 'what is the weather today?' --cwd ~/.hermes/self-evolution
~/.hermes/tools/hermes_claude_router.py --message 'Plan how to add a safe paired-win gate to the self-evolution engine' --cwd ~/.hermes/self-evolution
~/.hermes/tools/hermes_claude_router.py --message 'Implement a tiny test-only change in a worktree' --cwd ~/.hermes/self-evolution
~/.hermes/tools/hermes_claude_router.py --message 'Review this diff for safety issues' --cwd ~/.hermes/self-evolution
```

### Gateway hard-routing tests
```bash
cd ~/.hermes/hermes-agent
source venv/bin/activate
pytest -q tests/gateway/test_claude_code_routing.py
pytest -q tests/gateway/test_telegram_text_batching.py
```

---

## Current status

The hybrid system is now in a good state:

- wrapper auth path works
- delegate works
- `plan_minimal` works reliably
- router works
- hard routing exists behind env toggles
- `/md` chunking + plugin discovery are fixed in code

What is intentionally still staged:
- the router is not always-on unless you enable env toggles
- Claude Code patching still follows plan-first + confirmation
- no generic Claude provider path inside Hermes

That is the correct foundation.

---

## Repo-tracked files in this directory

- `scripts/claude-code/hermes-claude-opus-max`
- `scripts/claude-code/claude_code_delegate.py`
- `scripts/claude-code/hermes_claude_router.py`
- `scripts/claude-code/claude_control_plan.schema.json`
- `scripts/claude-code/minimal-settings.json`
- `scripts/claude-code/README.md`
- `skills/autonomous-ai-agents/claude-code-opus-max/SKILL.md`
- `skills/autonomous-ai-agents/claude-code-controller/SKILL.md`

These are the repo-friendly copies of the home-level integration assets.
