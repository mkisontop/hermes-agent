---
name: claude-code-opus-max
description: Delegate high-stakes coding, architecture, and review tasks to the official Claude Code CLI on the Mac mini using Claude Max subscription OAuth, Opus 4.7, and max effort. Use only when the task deserves maximum reasoning.
version: 1.0.0
metadata:
  hermes:
    risk: medium
    tags: [coding-agent, claude-code, opus, max-effort, mac-mini]
---

# Claude Code Opus Max Delegation

Use this skill when the user asks for difficult coding, architecture, refactoring, debugging, repository review, or implementation planning that benefits from maximum reasoning.

## Policy

Always invoke Claude Code through:

```
~/.hermes/bin/hermes-claude-opus-max
```

**Never** invoke raw `claude` from Hermes automation.

Never pass or request:
- `ANTHROPIC_API_KEY`
- `ANTHROPIC_AUTH_TOKEN`
- `ANTHROPIC_BASE_URL`
- Console/API credentials

Never use:
- `claude auth login --console`
- `--bare`
- `--dangerously-skip-permissions`
- unrestricted Bash
- `git push`
- `rm -rf`
- `sudo`

## Model profile

Default:
- model: `claude-opus-4-7`
- effort: `max`
- permission mode: selected by task
- output: JSON or stream-json
- no session persistence for automated runs

Fallback:
- if `claude-opus-4-7` is unavailable, use `--model opus`
- do **not** silently fall back to API-key auth

## Routing policy — when to call Claude Code

**Use Claude Code Opus Max for:**
- hard repo changes
- architecture decisions
- failing test diagnosis
- security-sensitive code review
- self-evolution engine changes
- large refactors
- anything where a wrong patch is expensive

**Do NOT use Opus Max for:**
- normal chat / routing
- quick one-file edits
- simple grep/find tasks
- routine summaries
- every nightly evolution run
- background loops
- raw search/retrieval

Hermes keeps cheap local models for routine work. Claude Code is the surgical tool.

## Preferred invocation — Python delegate

For Hermes automation, use:

```bash
~/.hermes/tools/claude_code_delegate.py <profile> "<prompt>" [--cwd DIR] [--worktree NAME] [--timeout SECS]
```

Profiles: `smoke`, `plan_minimal`, `plan_files`, `patch_worktree`, `review`.

Compatibility aliases may exist in the delegate (`plan` → `plan_minimal`, `patch` → `patch_worktree`), but Hermes automation should use the canonical names above.

## Modes

### `plan_minimal` — default planning mode

```bash
~/.hermes/tools/claude_code_delegate.py plan_minimal \
  "Produce an implementation plan for X. Do not edit files." \
  --cwd ~/.hermes/self-evolution --timeout 1800
```

Required characteristics (already baked into the delegate):
- `-p`
- `--model claude-opus-4-7`
- `--effort max`
- `--permission-mode plan`
- `--tools Read`
- `--allowedTools Read`
- `--disallowedTools Bash Edit Write WebFetch Grep Glob`
- `--disable-slash-commands`
- `--setting-sources project`
- `--strict-mcp-config --mcp-config '{"mcpServers":{}}'`
- `--settings ~/.hermes/claude-code/minimal-settings.json` when present (or `HERMES_CLAUDE_MINIMAL_SETTINGS` override)
- `--max-turns 6`
- `--output-format stream-json`
- `--verbose`
- `--include-partial-messages`
- `--no-session-persistence`

Use this for most planning/design/architecture requests. It is intentionally almost non-agentic: Read-only, no Bash, no repo touring.

### `plan_files` — explicit-file planning

```bash
~/.hermes/tools/claude_code_delegate.py plan_files \
  "Read only these files: ... Return the plan directly. Do not edit files." \
  --cwd ~/.hermes/self-evolution --timeout 1800
```

Same constraints as `plan_minimal`, but the prompt must provide an explicit file list. `plan_files` is for planning with repo context while still preventing broad exploration.

### `patch_worktree` — worktree-isolated implementation

```bash
~/.hermes/tools/claude_code_delegate.py patch_worktree \
  "Implement X. Preserve existing tests." \
  --cwd ~/.hermes/self-evolution --worktree task-name --timeout 7200
```

Required flags:
- `--worktree <task-name>` (mandatory; worktree required)
- `--permission-mode acceptEdits`
- `--tools Read,Grep,Glob,Edit,Write,Bash`
- explicit allowed Edit/Write/Bash
- explicit disallowed `git push`, `rm -rf`, `sudo`, `curl|sh`, `wget|sh`
- `--max-turns 24`
- `--output-format stream-json`
- `--verbose`
- `--include-partial-messages`
- `--no-session-persistence`

This is the **only** implementation profile Hermes automation should use.

### Review mode — read-only scrutiny after changes

```bash
~/.hermes/tools/claude_code_delegate.py review \
  "Perform an ultra-careful code review of the current diff..." \
  --cwd ~/.hermes/self-evolution --timeout 3600
```

Required flags:
- `--permission-mode plan`
- `--disallowedTools Edit, Write`
- `--max-turns 10`

## Delegate implementation notes learned in production

- The delegate must launch Claude with `stdin=subprocess.DEVNULL`; without that, headless plan runs can hit `no stdin data received in 3s` / `tcsetattr: Inappropriate ioctl for device` and produce empty logs.
- For automation, `stream-json --verbose --include-partial-messages` is safer than plain `json` because long print-mode runs can surface useful assistant content before the final result object. This was necessary to debug long Batch C planning runs.
- The valid empty strict MCP config on this installed CLI is:

```bash
--strict-mcp-config --mcp-config '{"mcpServers":{}}'
```

  Do **not** use `--mcp-config '{}'` here — Claude Code 2.1.116 rejects it as invalid schema.
- Broad planning profiles cause turn burn. Default planning should be `plan_minimal`; use `plan_files` only with an explicit file list.
- Keep `--bare` forbidden for the Max-subscription path. Bare mode skips OAuth/keychain reads and pushes auth toward API-key style flows.


After every Claude Code run, Hermes must report:
- command profile used
- model and effort
- auth status checked or assumed
- worktree name, if any
- files changed (from `git status` in that worktree)
- tests run
- exit code
- proposal for next action

**Never approve, merge, or push automatically.**

## Auth verification before first use in a session

```bash
unset ANTHROPIC_API_KEY ANTHROPIC_AUTH_TOKEN ANTHROPIC_BASE_URL ANTHROPIC_MODEL
~/.hermes/bin/hermes-claude-opus-max auth status --text
```

Expected:

```
Login method: Claude Max account
Organization: <your org>
Email: <your email>
```

If it says anything else (API key, Console login), stop and investigate before running.

## Smoke test

```bash
~/.hermes/tools/claude_code_delegate.py smoke "Return exactly PONG." --cwd ~/.hermes/self-evolution --timeout 120
```

Expected output: JSON with `result: "PONG"` and `exit_code: 0`.
