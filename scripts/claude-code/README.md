# Claude Code Hybrid System Assets

This directory contains the **repo-tracked copies** of the home-level Claude Code + Hermes integration artifacts.

These files are designed to be installed into `~/.hermes/` on a Hermes operator machine.

## Files

- `hermes-claude-opus-max` — safe wrapper that preserves Claude Max subscription auth and strips API-billing env vars
- `claude_code_delegate.py` — bounded multi-profile Claude Code delegate (`smoke`, `plan_minimal`, `plan_files`, `patch_worktree`, `review`)
- `hermes_claude_router.py` — intent router that classifies planning/coding/review/normal and invokes the delegate
- `claude_control_plan.schema.json` — structured routing/decision schema
- `minimal-settings.json` — minimal read-only planning settings for Claude Code automation

## Suggested installation

```bash
mkdir -p ~/.hermes/bin ~/.hermes/tools ~/.hermes/schemas ~/.hermes/claude-code
cp scripts/claude-code/hermes-claude-opus-max ~/.hermes/bin/
cp scripts/claude-code/claude_code_delegate.py ~/.hermes/tools/
cp scripts/claude-code/hermes_claude_router.py ~/.hermes/tools/
cp scripts/claude-code/claude_control_plan.schema.json ~/.hermes/schemas/
cp scripts/claude-code/minimal-settings.json ~/.hermes/claude-code/
chmod +x ~/.hermes/bin/hermes-claude-opus-max ~/.hermes/tools/claude_code_delegate.py ~/.hermes/tools/hermes_claude_router.py
```

For the full architecture, routing policy, verification sequence, and safety model, read:

- `docs/claude-code/README.md`
