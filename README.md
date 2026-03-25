# Compact Guard - Proactive Compaction for Claude Code

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Claude Code](https://img.shields.io/badge/Claude%20Code-hooks-blueviolet)](https://code.claude.com/docs/en/hooks)

Save money by compacting context while the API cache is still warm.

<!-- Record a short demo and replace this with: ![Demo](assets/demo.gif) -->
<!-- Recommended: 10-15 sec GIF showing the VS Code dialog pop up + one-click compact -->

## The Problem

Claude Code's auto-compact only triggers when a **new message** is sent and context exceeds ~83%.
If you wait 4+ minutes between messages, the prompt cache expires. That means your next message
sends the entire conversation (e.g. 160K tokens) **without cache** — costing significantly more
(or eating 10x faster your Pro/Max plan quota).

## The Solution

Three components that work together:

1. **StatusLine hook** (`hooks/context-monitor.js`) — the primary data source. Receives live
   `context_window` and `rate_limits` data from Claude Code on every streaming update. Writes
   per-session metrics to disk (context %, token counts, session/weekly usage, `last_interaction_time`)
   and displays a color-coded context bar in the terminal. Fully self-contained — works without the other components.
2. **PostToolUse hook** (`hooks/compact-check.py`) — fires after every tool call **in both CLI
   and VS Code/Cursor modes**. Much more frequent than a Stop hook. Reads metrics written by
   StatusLine hook, can also parse the transcript as fallback. Merges data and writes metrics
   so the extension always has fresh info. Sets `last_interaction_time` on each run (the cache TTL
   countdown anchor). Without StatusLine hook data, it operates in degraded mode with inferred values.
3. **VS Code / Cursor extension** (`vscode-extension/`) — polls metrics every 10s for status bar
   display, shows cache countdown timer, and prompts "Run /compact" dialog when cache is about to
   expire and context is at danger level.

## How It Works

```
Claude Code CLI
    │
    ├─► StatusLine hook (context-monitor.js)   fires on every token stream update
    │       writes: metrics-{session_id}.json  (context %, tokens, rate limits, last_interaction_time)
    │       prints: colored status bar → stdout → Claude Code displays it
    │
    └─► PostToolUse hook (compact-check.py)     fires after every tool call
            reads:  metrics-{session_id}.json  (for context_window_size, rate limits)
            writes: metrics-{session_id}.json  (merged: transcript tokens + cached fields)

VS Code Extension (extension.js)
    ├─ writes: claude-code-compact-guard-active   heartbeat every 10s
    └─ reads:  metrics-{session_id}.json          polls every 10s → status bar + cache countdown
```

**Component dependencies:**
- StatusLine hook is fully self-contained — works without the other components.
- PostToolUse hook and extension depend on StatusLine hook for accurate data (real context window size,
  rate limits, session/weekly usage). Without it, they operate in degraded mode with inferred values.
- Extension reads metrics written by either hook — whichever ran most recently.

**Cache TTL:** Anthropic's prompt cache TTL is ~3–5 minutes (historically 5 min, reports suggest
~3 min since late 2025). The cache refreshes on every cache hit, so the timer resets with each
request. We use **4 minutes** as the working estimate. The `last_interaction_time` in metrics is
set by context-monitor.js (on token count changes) and by compact-check.py (on each PostToolUse run),
giving the extension an accurate basis for the cache countdown.

## Quick Install

```bash
git clone https://github.com/anthropics/claude-code-compact-guard.git
cd claude-code-compact-guard
bash install.sh
```

The installer will:
- Copy hook scripts to `~/.claude/hooks/`
- Patch `~/.claude/settings.json` (with backup)
- Install the `.vsix` extension in VS Code, Cursor, and VS Code Insiders (whichever are found)

Then **restart Claude Code** and **reload your editor** (`Developer: Reload Window`).

## Manual Install

### Step 1: Copy scripts

```bash
mkdir -p ~/.claude/hooks
cp hooks/context-monitor.js ~/.claude/hooks/
cp hooks/compact-check.py ~/.claude/hooks/
chmod +x ~/.claude/hooks/context-monitor.js
chmod +x ~/.claude/hooks/compact-check.py
```

### Step 2: Edit `~/.claude/settings.json`

Add or merge the following into your existing settings:

```json
{
  "statusLine": {
    "type": "command",
    "command": "node ~/.claude/hooks/context-monitor.js",
    "padding": 0
  },
  "hooks": {
    "PostToolUse": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "python3 ~/.claude/hooks/compact-check.py"
          }
        ]
      }
    ]
  }
}
```

If you already have a `statusLine` or hooks, merge them manually —
don't replace the whole file.

### Step 3: Install the extension

```bash
# VS Code
code --install-extension compact-guard-0.1.1.vsix --force

# Cursor
cursor --install-extension compact-guard-0.1.1.vsix --force

# VS Code Insiders
code-insiders --install-extension compact-guard-0.1.1.vsix --force

# Windsurf
windsurf --install-extension compact-guard-0.1.1.vsix --force
```

### Step 4: Restart everything

- Restart Claude Code (quit and reopen, or start a new session)
- Reload editor: `Cmd+Shift+P` -> `Developer: Reload Window`
- Verify hooks: run `/hooks` in Claude Code to see your configured hooks

## Configuration

### Token thresholds

Defined in `context-monitor.js` and `compact-check.py` (keep in sync):

```python
AUTOCOMPACT_BUFFER_TOKENS = 33_000  # Claude Code reserves this; subtracted from raw window
WARN_TOKENS = 60_000                # status turns yellow/warning
COMPACT_TOKENS = 100_000            # status turns orange/danger, extension may prompt compact
```

### Cache timing

Defined in `extension.js`:

```javascript
const CACHE_TTL_SECONDS = 240;   // 4 min — prompt cache expiry estimate
const CACHE_WARN_SECONDS = 90;   // show compact dialog when this much cache time remains
```

### Recommended thresholds

| Style | COMPACT_TOKENS | Notes |
|-------|---------------|-------|
| Aggressive (cheapest) | 60K | Frequent compaction, short context |
| Balanced | 100K | Good tradeoff for most workflows |
| Conservative | 120K | More context, higher risk of expensive uncached calls |

## What happens in practice

1. You chat with Claude, context grows
2. Each tool call fires the PostToolUse hook, which writes fresh metrics
3. Extension polls metrics every 10s, shows context % and cache countdown in status bar
4. Cache countdown approaches expiry while context is at danger level (≥100K tokens)
5. Extension shows warning dialog: "Cache expires in ~60s — Compact now to save costs?"
6. You click "Run /compact" → extension sends `/compact` to Claude Code terminal
7. Compaction runs using cached tokens (cheap!)
8. Context drops to ~5–10%
9. You continue working

If you ignore the warning:
- Cooldown prevents nagging for ~200 seconds
- Claude's built-in auto-compact still fires at ~83% as usual
- But by then context is large and possibly uncached — exactly what we're trying to avoid

## Extension Features

**Cache countdown timer** — shows time remaining until the prompt cache expires, based on
`last_interaction_time` from hooks. Disappears when cache has expired (no stale "expired" shown).

**Warning dialog** — native VS Code/Cursor warning notification with two buttons:
- "Run /compact" — finds the Claude Code terminal and sends the command automatically
- "Dismiss" — ignore this time (cooldown still applies)

**Status bar** — shows context percentage and cache countdown:
- `$(check) 25% | 3:42` — healthy, cache warm
- `$(warning) 45% | 1:15` — approaching threshold
- `$(error) 70% | 0:30` — high context, cache expiring soon

Hover the status bar item to see full details: `Context: 25% (50K/167K) | Cache: 3:42 remaining | Session: 11% (resets in 3h 42m) | Weekly: 36%`

Updates every 10 seconds when a Claude Code session is active.

**Commands** (accessible via `Cmd+Shift+P` / `Ctrl+Shift+P`):
- `Compact Guard: Run /compact in Claude Code` — manually trigger compaction
- `Compact Guard: Show Context Status` — show current context usage

**Terminal detection** — the extension looks for terminals with "claude" in the name.
If not found, it focuses the Claude Code VS Code extension and copies `/compact` to clipboard.

## Limitations

- **Cannot auto-trigger /compact from CLI hooks** — Claude Code doesn't expose `/compact` as
  a programmable action from hooks. The VS Code/Cursor extension solves this via `terminal.sendText`.
- **StatusLine is the primary data source** — the PostToolUse hook can parse transcripts as fallback,
  but lacks real `context_window_size` and `rate_limits` without StatusLine hook data.
- **Cache TTL is approximate** — Anthropic doesn't document the exact TTL; it may vary
  (~3–5 minutes, we use 4 min as the working estimate).

## Files

```
compact-guard/
├── hooks/
│   ├── context-monitor.js       # StatusLine — writes metrics, shows context bar
│   └── compact-check.py         # PostToolUse hook — merges transcript data, writes metrics
├── vscode-extension/            # VS Code / Cursor extension source
│   ├── extension.js
│   └── package.json
├── install.sh                   # Installer (hooks + settings + extension)
└── .github/workflows/
    └── release.yml              # CI/CD — builds .vsix and creates GitHub release
```

Installed locations:
```
~/.claude/hooks/
├── context-monitor.js
└── compact-check.py
```

Temp files (auto-managed, in `$TMPDIR/claude-code-compact-guard/`):

| File | Writer | Reader | Purpose |
|---|---|---|---|
| `metrics-{session_id}.json` | context-monitor.js, compact-check.py | compact-check.py, extension | Context %, tokens, rate limits, last_interaction_time |
| `claude-code-compact-guard-active` | extension.js | — | Heartbeat proving extension is alive |

Override temp dir with `COMPACT_GUARD_TMPDIR` env var. Heartbeat file is directly in `$TMPDIR/`.

## Uninstall

```bash
# Remove hooks
rm ~/.claude/hooks/context-monitor.js
rm ~/.claude/hooks/compact-check.py

# Remove extension
code --uninstall-extension philipshurpik.claude-code-compact-guard
cursor --uninstall-extension philipshurpik.claude-code-compact-guard
windsurf --uninstall-extension philipshurpik.claude-code-compact-guard

# Clean up temp files
rm -rf /tmp/claude-code-compact-guard /tmp/claude-code-compact-guard-active
```

Then remove the `statusLine` and `PostToolUse` hook entries from `~/.claude/settings.json`.
