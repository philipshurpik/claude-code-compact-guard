# Compact Guard - Proactive Compaction for Claude Code

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Claude Code](https://img.shields.io/badge/Claude%20Code-hooks-blueviolet)](https://code.claude.com/docs/en/hooks)

Save money by compacting context while the API cache is still warm.

<!-- Record a short demo and replace this with: ![Demo](assets/demo.gif) -->
<!-- Recommended: 10-15 sec GIF showing the VS Code dialog pop up + one-click compact -->

## The Problem

Claude Code's auto-compact only triggers when a **new message** is sent and context exceeds ~83%.
If you wait 5+ minutes between messages, the prompt cache expires. That means your next message
sends the entire conversation (e.g. 160K tokens) **without cache** - costing significantly more.

## The Solution

Three components that work together:

1. **StatusLine** (`context-monitor.js`) - monitors context usage in real time, writes metrics
   to a temp file, and displays a color-coded context bar in the terminal
2. **Stop hook** (`compact-check.py`) - fires immediately after Claude finishes responding.
   If context exceeds your threshold, it blocks Claude from stopping and tells it to ask you
   to run `/compact` - while the cache is still hot. Works in terminal / CLI.
3. **VS Code / Cursor extension** (`compact-guard-0.1.0.vsix`) - shows a native warning dialog
   with a "Run /compact" button that sends the command directly to the Claude Code terminal.
   No manual typing needed.

## How It Works

**Terminal (CLI):**
```
Claude responds -> Stop hook fires -> reads context metrics
                                   -> if > 40%: blocks Claude, shows warning
                                   -> Claude tells you: "run /compact now"
                                   -> you type /compact (cache is still warm!)
```

**VS Code / Cursor:**
```
Claude responds -> Stop hook fires -> writes trigger file
                                   -> extension sees trigger
                                   -> shows warning dialog with "Run /compact" button
                                   -> you click the button
                                   -> extension sends /compact to terminal automatically
```

Both paths fire simultaneously - you get the CLI warning AND the editor dialog.

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
cp context-monitor.js ~/.claude/hooks/
cp compact-check.py ~/.claude/hooks/
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
# When to suggest compaction (percentage of context used)
COMPACT_THRESHOLD_PCT = 40

# Don't nag more than once per N seconds
COOLDOWN_SECONDS = 120
```

Edit `~/.claude/hooks/context-monitor.js`:

```javascript
// StatusLine color thresholds
const WARN_PCT = 40;   // yellow
const DANGER_PCT = 60;  // red
```

### Recommended thresholds

| Style | Stop hook threshold | Notes |
|-------|-------------------|-------|
| Aggressive (cheapest) | 30% | Frequent compaction, short context |
| Balanced | 40-50% | Good tradeoff for most workflows |
| Conservative | 65% | More context, higher risk of expensive uncached calls |

## What happens in practice

1. You chat with Claude, context grows
2. Claude finishes a response at 42% context
3. Stop hook fires, reads metrics, sees 42% > 40%
4. Two things happen simultaneously:
   - **CLI**: hook returns `{"decision": "block"}`, Claude warns you in chat
   - **Editor**: hook writes trigger file, extension shows warning dialog
5. In VS Code / Cursor, you click "Run /compact"
6. Extension sends `/compact` to the Claude Code terminal
7. Compaction runs using cached tokens (cheap!)
8. Context drops to ~5-10%
9. You continue working

If you ignore the warning:
- Cooldown prevents nagging for 2 minutes
- Claude's built-in auto-compact still fires at ~83% as usual
- But by then context is large and possibly uncached - exactly what we're trying to avoid

## Extension Features

**Warning dialog** - native VS Code/Cursor warning notification with two buttons:
- "Run /compact" - finds the Claude Code terminal and sends the command automatically
- "Dismiss" - ignore this time (cooldown still applies)

**Status bar** - shows context percentage in the bottom bar with icons:
- `$(check) Ctx: 25%` when healthy
- `$(info) Ctx: 45%` when approaching threshold
- `$(warning) Ctx: 70%` when high

Updates every 3 seconds when a Claude Code session is active.

**Commands** (accessible via `Cmd+Shift+P` / `Ctrl+Shift+P`):
- `Compact Guard: Run /compact in Claude Code` - manually trigger compaction
- `Compact Guard: Show Context Status` - show current context usage

**Terminal detection** - the extension looks for terminals with "claude" or "cc" in the name.
If not found, it falls back to the active terminal. If no terminal is found at all, it copies
`/compact` to clipboard for manual pasting.

## Limitations

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
├── context-monitor.js           # StatusLine - writes metrics, shows context bar
├── compact-check.py             # Stop hook - prompts compaction + triggers extension
├── install.sh                   # Installer (hooks + settings + extension)
├── compact-guard-0.1.0.vsix     # Pre-built VS Code / Cursor extension
└── vscode-extension/            # Extension source code
    ├── extension.js
    ├── package.json
    └── ...
```

Installed locations:
```
~/.claude/hooks/
├── context-monitor.js
└── compact-check.py
```

Temp files (auto-managed):
- `/tmp/claude-context-metrics.json` - context metrics (written by StatusLine)
- `/tmp/claude-compact-trigger.json` - extension trigger (written by Stop hook)
- `/tmp/claude-compact-cooldown` - cooldown marker

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
rm -f /tmp/claude-context-metrics.json /tmp/claude-compact-trigger.json /tmp/claude-compact-cooldown
```

Then remove the `statusLine` and `Stop` hook entries from `~/.claude/settings.json`.
