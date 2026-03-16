# Compact Guard - Proactive Compaction for Claude Code

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Claude Code](https://img.shields.io/badge/Claude%20Code-hooks-blueviolet)](https://code.claude.com/docs/en/hooks)

Save money by compacting context while the API cache is still warm.

<!-- Record a short demo and replace this with: ![Demo](assets/demo.gif) -->
<!-- Recommended: 10-15 sec GIF showing the VS Code dialog pop up + one-click compact -->

## The Problem

Claude Code's auto-compact only triggers when a **new message** is sent and context exceeds ~83%.
If you wait 5+ minutes between messages, the prompt cache expires. That means your next message
sends the entire conversation (e.g. 160K tokens) **without cache** - costing significantly more 
(or eating 10 times faster your Pro/Max plan quota).

## The Solution

Three components that work together:

1. **StatusLine** (`hooks/context-monitor.js`) - monitors context usage in real time, writes metrics
   to a temp file, and displays a color-coded context bar in the terminal
2. **Stop hook** (`hooks/compact-check.py`) - fires immediately after Claude finishes responding.
   If context exceeds your threshold, it blocks Claude from stopping and tells it to ask you
   to run `/compact` - while the cache is still hot. Works in terminal / CLI.
3. **VS Code / Cursor extension** (`vscode-extension/`) - shows a native warning dialog
   with a "Run /compact" button that sends the command directly to the Claude Code terminal.
   No manual typing needed.

## How It Works

**VS Code / Cursor** (extension installed):
```
Claude responds -> Stop hook fires -> detects extension is active
                                   -> writes trigger file, does NOT block Claude
                                   -> extension sees trigger
                                   -> shows warning dialog with "Run /compact" button
                                   -> you click the button
                                   -> extension sends /compact to terminal automatically
```

**Terminal (CLI)** (no extension):
```
Claude responds -> Stop hook fires -> no extension heartbeat detected
                                   -> blocks Claude, shows warning
                                   -> Claude tells you: "run /compact now"
                                   -> you type /compact (cache is still warm!)
```

The stop hook auto-detects whether the extension is running. With the extension,
Claude isn't blocked -- you just get a clean dialog. Without it, you get the CLI fallback.

## Architecture

The three components never talk to each other directly — all communication is via temp files in `$TMPDIR/`:

```
Claude Code CLI
    │
    ├─► StatusLine hook (context-monitor.js)   fires on every token stream update
    │       reads:  usage-cache.json           (written by Stop hook, 300s TTL)
    │       writes: metrics-{session_id}.json  (context % + session/weekly usage)
    │       prints: colored status bar → stdout → Claude Code displays it
    │
    └─► Stop hook (compact-check.py)           fires after every response
            reads:  metrics-{session_id}.json
            writes: usage-cache.json           (OAuth API fetch)
                    cooldown-{session_id}      (rate-limit marker)
                    trigger.json              (if extension heartbeat is fresh)

VS Code Extension (extension.js)
    ├─ writes: claude-code-compact-guard-active   heartbeat every 3s
    ├─ reads:  metrics-{session_id}.json          polls every 3s → status bar
    └─ watches: trigger.json
                    → shows "Run /compact?" dialog
                    → sends /compact to Claude terminal
```

Session usage data (five-hour quota, weekly quota, resets_at) flows backwards through the cache:
Stop hook fetches the OAuth API → writes `usage-cache.json` → StatusLine reads it and includes
`session_usage_pct`, `session_resets_at`, `weekly_usage_pct` in the metrics file → extension
displays these in the status bar tooltip and the "Show Context Status" notification.

## Quick Install

```bash
git clone https://github.com/YOUR_USERNAME/claude-code-compact-guard.git
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
    "Stop": [
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

If you already have a `statusLine` or hooks, merge them manually -
don't replace the whole file.

### Step 3: Install the extension

```bash
# VS Code
code --install-extension compact-guard-0.1.0.vsix --force

# Cursor
cursor --install-extension compact-guard-0.1.0.vsix --force

# VS Code Insiders
code-insiders --install-extension compact-guard-0.1.0.vsix --force

# Windsurf
windsurf --install-extension compact-guard-0.1.0.vsix --force
```

### Step 4: Restart everything

- Restart Claude Code (quit and reopen, or start a new session)
- Reload editor: `Cmd+Shift+P` -> `Developer: Reload Window`
- Verify hooks: run `/hooks` in Claude Code to see your configured hooks

## Configuration

### Thresholds

Edit `~/.claude/hooks/compact-check.py`:

```python
# When to suggest compaction (absolute input tokens)
COMPACT_THRESHOLD_TOKENS = 80_000

# Don't nag more than once per N seconds
COOLDOWN_SECONDS = 200
```

Edit `~/.claude/hooks/context-monitor.js`:

```javascript
// StatusLine color thresholds (absolute input tokens)
const WARN_TOKENS = 60_000;    // yellow
const DANGER_TOKENS = 80_000;  // orange
```

### Recommended thresholds

| Style | Stop hook threshold | Notes |
|-------|-------------------|-------|
| Aggressive (cheapest) | 60K tokens | Frequent compaction, short context |
| Balanced | 80K tokens | Good tradeoff for most workflows |
| Conservative | 120K tokens | More context, higher risk of expensive uncached calls |

## What happens in practice

1. You chat with Claude, context grows
2. Claude finishes a response at 42% context
3. Stop hook fires, reads metrics, sees token count > 80K
4. **With extension**: hook writes trigger file and exits cleanly (no block).
   Extension shows warning dialog with "Run /compact" button.
   **Without extension**: hook blocks Claude, which warns you in chat.
5. You click "Run /compact" in the dialog (or type `/compact` in CLI)
6. Compaction runs using cached tokens (cheap!)
7. Context drops to ~5-10%
8. You continue working

If you ignore the warning:
- Cooldown prevents nagging for ~3 minutes
- Claude's built-in auto-compact still fires at ~83% as usual
- But by then context is large and possibly uncached - exactly what we're trying to avoid

## Extension Features

**Warning dialog** - native VS Code/Cursor warning notification with two buttons:
- "Run /compact" - finds the Claude Code terminal and sends the command automatically
- "Dismiss" - ignore this time (cooldown still applies)

**Status bar** - shows context percentage in the bottom bar with icons:
- `$(check) Ctx: 25% | S: 11%` when healthy
- `$(info) Ctx: 45% | S: 30%` when approaching threshold
- `$(warning) Ctx: 70% | S: 55%` when high

Hover the status bar item to see full details: `Context: 25% | Session: 11% (resets in 3h 42m) | Weekly: 36%`

Updates every 3 seconds when a Claude Code session is active.

**Commands** (accessible via `Cmd+Shift+P` / `Ctrl+Shift+P`):
- `Compact Guard: Run /compact in Claude Code` - manually trigger compaction
- `Compact Guard: Show Context Status` - show current context usage

**Terminal detection** - the extension looks for terminals with "claude" or "cc" in the name.
If not found, it falls back to the active terminal. If no terminal is found at all, it copies
`/compact` to clipboard for manual pasting.

## Limitations

- **macOS only for session usage tracking** - The Stop hook reads OAuth credentials
  from the macOS Keychain (`security` CLI) to fetch session/weekly usage quota from the Anthropic API.
  On non-macOS systems, context monitoring and compaction still work fully — only the
  session % and weekly % indicators will be absent.
- **Cannot auto-trigger /compact from CLI hooks** - Claude Code doesn't expose `/compact` as
  a programmable action from hooks. The Stop hook can only ask Claude to tell you.
  The VS Code/Cursor extension solves this via `terminal.sendText`.
- **StatusLine is the only live context monitor** - the Stop hook itself doesn't
  receive token counts, so we bridge via a temp file written by StatusLine.
- **5-minute cache TTL is approximate** - Anthropic doesn't document the exact
  TTL, it may vary.

## Files

```
compact-guard/
├── hooks/
│   ├── context-monitor.js       # StatusLine - writes metrics, shows context bar
│   └── compact-check.py         # Stop hook - prompts compaction + triggers extension
├── vscode-extension/            # VS Code / Cursor extension source
│   ├── extension.js
│   └── package.json
├── install.sh                   # Installer (hooks + settings + extension)
└── .github/workflows/
    └── release.yml              # CI/CD - builds .vsix and creates GitHub release
```

Installed locations:
```
~/.claude/hooks/
├── context-monitor.js
└── compact-check.py
```

Temp files (auto-managed, in `/tmp/`):
- `claude-code-compact-guard/metrics-{session_id}.json` - per-session context + usage metrics (written by StatusLine)
- `claude-code-compact-guard/usage-cache.json` - OAuth API response cache, 300s TTL (written by Stop hook)
- `claude-code-compact-guard/cooldown-{session_id}` - per-session cooldown marker
- `claude-code-compact-guard-trigger.json` - extension trigger (written by Stop hook)
- `claude-code-compact-guard-active` - extension heartbeat (tells Stop hook to skip blocking)

## Uninstall

```bash
# Remove hooks
rm ~/.claude/hooks/context-monitor.js
rm ~/.claude/hooks/compact-check.py

# Remove extension
code --uninstall-extension compact-guard.compact-guard
cursor --uninstall-extension compact-guard.compact-guard
windsurf --uninstall-extension compact-guard.compact-guard

# Clean up temp files
rm -rf /tmp/claude-code-compact-guard /tmp/claude-code-compact-guard-trigger.json /tmp/claude-code-compact-guard-active
```

Then remove the `statusLine` and `Stop` hook entries from `~/.claude/settings.json`.
