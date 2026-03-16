# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

A cost-saving tool for Claude Code that monitors context window usage and proactively triggers `/compact` before the prompt cache expires (~5 min TTL). Three cooperating components communicate via temp files in `$TMPDIR/claude-code-compact-guard/`:

1. **StatusLine hook** (`hooks/context-monitor.js`, Node.js) — receives live `context_window` data from Claude Code, writes per-session metrics to disk, outputs color-coded status bar.
2. **Stop hook** (`hooks/compact-check.py`, Python) — fires after every response, reads metrics, decides whether to prompt compaction (threshold: 40%, cooldown: 200s). In CLI mode it blocks Claude with a JSON decision; with the extension present it writes a trigger file instead.
3. **VS Code extension** (`vscode-extension/extension.js`) — heartbeats every 3s to prove it's alive, watches for trigger files, shows native "Run /compact" dialog, sends `/compact` to the Claude terminal.

Extension detection: Stop hook checks heartbeat file freshness (<30s). `TERM_PROGRAM` env var is no longer used — heartbeat alone is the reliable signal.

**OAuth usage API quirk:** The StatusLine hook fetches session usage from `api.anthropic.com/api/oauth/usage`. On 429 errors, it refreshes the OAuth token and retries — this is an intentional workaround because 429 from this endpoint typically means the token needs re-authentication, not a standard rate limit.

## Commands

```bash
# Run python tests
python3 -m pytest tests/test_compact_check.py

# Run single Python test
python3 -m pytest tests/test_compact_check.py -k "test_name"

# Run JS tests
node --test tests/test_context_monitor.js

# Build extension
cd vscode-extension && npx @vscode/vsce package --allow-missing-repository
```

## Testing Approach

- Python tests (`tests/test_compact_check.py`): run the Stop hook as a subprocess with controlled `COMPACT_GUARD_TMPDIR` and `TERM_PROGRAM` env vars. Use `tmp_path` for isolation.
- JS tests (`tests/test_context_monitor.js`): use `node:test` (no deps), run StatusLine hook via `execFileSync`.
- CI: GitHub Actions on push/PR to main — Node 20, Python 3.12, ruff lint+format check, both test suites.

## Temp File Communication

| File | Writer | Reader | Purpose |
|---|---|---|---|
| `metrics-{session_id}.json` | context-monitor.js | compact-check.py, extension | Context metrics |
| `cooldown-{session_id}` | compact-check.py | compact-check.py | Rate-limit per session |
| `claude-code-compact-guard-trigger.json` | compact-check.py | extension.js | Signal to show dialog |
| `claude-code-compact-guard-active` | extension.js | compact-check.py | Heartbeat proving extension is alive |

All under `$TMPDIR/claude-code-compact-guard/` (override with `COMPACT_GUARD_TMPDIR` env var), except heartbeat and trigger which are directly in `$TMPDIR/`.
