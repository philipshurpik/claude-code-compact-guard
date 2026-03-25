# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

A cost-saving tool for Claude Code that monitors context window usage and proactively triggers `/compact` before the prompt cache expires (~4 min TTL). Three cooperating components communicate via temp files in `$TMPDIR/claude-code-compact-guard/`:

1. **StatusLine hook** (`hooks/context-monitor.js`, Node.js) — the primary data source. Receives live `context_window` and `rate_limits` data from Claude Code on every streaming update. Writes per-session metrics to disk (context %, token counts, session/weekly usage, `last_interaction_time`) and outputs a color-coded status bar to the CLI. **Only fires in CLI mode — does NOT fire when using the VS Code/Cursor extension.** Fully self-contained — works without the other components.
2. **PostToolUse hook** (`hooks/compact-check.py`, Python) — fires after every tool call **in both CLI and VS Code/Cursor modes**. Much more frequent than Stop (fires per tool call, not per response). Reads metrics written by StatusLine hook, can also parse the transcript as fallback. Merges data and writes metrics so the extension always has fresh info. Sets `last_interaction_time` on each run (the cache TTL countdown anchor). Without StatusLine hook data, it lacks real `context_window_size` and `rate_limits` (infers from model ID).
3. **VS Code extension** (`vscode-extension/extension.js`) — polls metrics every 10s for status bar display, shows cache countdown timer, and prompts "Run /compact" dialog when cache is about to expire and context is at danger level. Writes heartbeat file every 10s (on each poll).

**Component dependencies:**
- StatusLine hook is fully self-contained — works without the other components. **CLI only.**
- PostToolUse hook fires after every tool call in both CLI and VS Code modes. Depends on StatusLine hook for accurate data (real context window size, rate limits, session/weekly usage). Without it (i.e., in VS Code mode), operates in degraded mode with inferred values from transcript.
- Extension reads metrics written by either hook — whichever ran most recently.

**Cache TTL:** Anthropic's prompt cache TTL is ~3-5 minutes (historically 5 min, reports suggest ~3 min since late 2025). The cache refreshes on every cache hit, so the timer resets with each request. We use 4 minutes as the working estimate. The `last_interaction_time` in metrics is set by context-monitor.js (on token count changes) and by compact-check.py (on each stop hook run), giving the extension an accurate basis for the cache countdown.

## Architecture

Components never talk to each other directly — all communication is via temp files:

```
Claude Code CLI
    │
    ├─► StatusLine hook (context-monitor.js)   fires on every token stream update
    │       writes: metrics-{session_id}.json  (context %, tokens, rate limits, last_interaction_time)
    │       prints: colored status bar → stdout → Claude Code displays it
    │
    └─► PostToolUse hook (compact-check.py)      fires after every tool call
            reads:  metrics-{session_id}.json  (for context_window_size, rate limits)
            writes: metrics-{session_id}.json  (merged: transcript tokens + cached fields)

VS Code Extension (extension.js)
    ├─ writes: claude-code-compact-guard-active   heartbeat every 3s
    └─ reads:  metrics-{session_id}.json          polls every 10s → status bar + cache countdown
```

**Token thresholds** (defined in context-monitor.js and compact-check.py):
- `AUTOCOMPACT_BUFFER_TOKENS` = 33K — Claude Code reserves this for autocompact; subtracted from raw window to get effective window size
- `WARN_TOKENS` = 60K — status turns yellow/warning
- `COMPACT_TOKENS` = 100K — status turns orange/danger, extension may prompt compact

**Cache timing** (defined in extension.js):
- `CACHE_TTL_SECONDS` = 240 (4 min) — prompt cache expiry estimate
- `CACHE_WARN_SECONDS` = 90 — show compact dialog when this much cache time remains

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

- Python tests (`tests/test_compact_check.py`): run the PostToolUse hook as a subprocess with controlled `COMPACT_GUARD_TMPDIR` and `TERM_PROGRAM` env vars. Use `tmp_path` for isolation.
- JS tests (`tests/test_context_monitor.js`): use `node:test` (no deps), run StatusLine hook via `execFileSync`.
- CI: GitHub Actions on push/PR to main — Node 20, Python 3.12, ruff lint+format check, both test suites.

## Temp File Communication

| File | Writer | Reader | Purpose |
|---|---|---|---|
| `metrics-{session_id}.json` | context-monitor.js, compact-check.py | compact-check.py, extension | Context %, tokens, rate limits, last_interaction_time |
| `claude-code-compact-guard-active` | extension.js | — | Heartbeat proving extension is alive |

All under `$TMPDIR/claude-code-compact-guard/` (override with `COMPACT_GUARD_TMPDIR` env var), except heartbeat which is directly in `$TMPDIR/`.
