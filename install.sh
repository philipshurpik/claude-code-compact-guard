#!/bin/bash
set -euo pipefail

# Compact Guard - installer for Claude Code proactive compaction hooks
# Copies scripts to ~/.claude/hooks/ and patches settings.json

HOOKS_DIR="$HOME/.claude/hooks"
SETTINGS_FILE="$HOME/.claude/settings.json"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "=== Compact Guard Installer ==="
echo ""

# 1. Create hooks directory
mkdir -p "$HOOKS_DIR"
echo "✓ Created $HOOKS_DIR"

# 2. Copy scripts
cp "$SCRIPT_DIR/hooks/context-monitor.js" "$HOOKS_DIR/context-monitor.js"
cp "$SCRIPT_DIR/hooks/compact-check.py" "$HOOKS_DIR/compact-check.py"
chmod +x "$HOOKS_DIR/context-monitor.js"
chmod +x "$HOOKS_DIR/compact-check.py"
echo "✓ Copied scripts to $HOOKS_DIR"

# 3. Patch settings.json
if [ ! -f "$SETTINGS_FILE" ]; then
    echo '{}' > "$SETTINGS_FILE"
    echo "✓ Created $SETTINGS_FILE"
fi

# Backup existing settings
cp "$SETTINGS_FILE" "$SETTINGS_FILE.backup.$(date +%s)"
echo "✓ Backed up settings.json"

# Use Python to merge settings (available everywhere Claude Code runs)
python3 - "$SETTINGS_FILE" "$HOOKS_DIR" <<'PYTHON_SCRIPT'
import json
import sys

settings_path = sys.argv[1]
hooks_dir = sys.argv[2]

with open(settings_path) as f:
    settings = json.load(f)

# Add StatusLine
settings['statusLine'] = {
    'type': 'command',
    'command': f'node {hooks_dir}/context-monitor.js',
    'padding': 0,
}
print('  + Set statusLine -> context-monitor.js')

# Add Stop hook (preserve existing hooks)
hooks = settings.setdefault('hooks', {})
stop_hooks = hooks.setdefault('Stop', [])

# Check if our hook is already installed
compact_check_cmd = f'python3 {hooks_dir}/compact-check.py'
already_installed = any(
    compact_check_cmd in str(group)
    for group in stop_hooks
)

if not already_installed:
    stop_hooks.append({
        'hooks': [
            {
                'type': 'command',
                'command': compact_check_cmd,
            }
        ]
    })
    print('  + Added Stop hook -> compact-check.py')
else:
    print('  ~ Stop hook already installed, skipping')

with open(settings_path, 'w') as f:
    json.dump(settings, f, indent=2)

PYTHON_SCRIPT

echo ""
echo "✓ Updated $SETTINGS_FILE"

# 4. Build and install VS Code / Cursor extension
VSIX_FILE="$SCRIPT_DIR/compact-guard-0.1.0.vsix"
EXT_DIR="$SCRIPT_DIR/vscode-extension"
INSTALLED_EDITORS=""

# Auto-build VSIX from source if not present
if [ ! -f "$VSIX_FILE" ] && [ -d "$EXT_DIR" ]; then
    echo ""
    if command -v npx &>/dev/null; then
        echo "Building extension from source..."
        (cd "$EXT_DIR" && npm install --save-dev @vscode/vsce 2>/dev/null && npx @vscode/vsce package --allow-missing-repository && mv *.vsix "$SCRIPT_DIR/") && \
            echo "✓ Built $VSIX_FILE" || \
            echo "✗ Failed to build extension (run 'cd vscode-extension && npx @vscode/vsce package' to see errors)"
    else
        echo "⚠️  npx not found - cannot build extension. Install Node.js or download .vsix from GitHub releases."
    fi
fi

if [ -f "$VSIX_FILE" ]; then
    echo ""
    # Try VS Code
    if command -v code &>/dev/null; then
        echo "Installing extension in VS Code..."
        if code --install-extension "$VSIX_FILE" --force 2>/dev/null; then
            INSTALLED_EDITORS="${INSTALLED_EDITORS}VS Code, "
            echo "✓ Installed in VS Code"
        else
            echo "✗ Failed to install in VS Code (try manually: code --install-extension $VSIX_FILE)"
        fi
    fi

    # Try Cursor
    if command -v cursor &>/dev/null; then
        echo "Installing extension in Cursor..."
        if cursor --install-extension "$VSIX_FILE" --force 2>/dev/null; then
            INSTALLED_EDITORS="${INSTALLED_EDITORS}Cursor, "
            echo "✓ Installed in Cursor"
        else
            echo "✗ Failed to install in Cursor (try manually: cursor --install-extension $VSIX_FILE)"
        fi
    fi

    # Try code-insiders
    if command -v code-insiders &>/dev/null; then
        echo "Installing extension in VS Code Insiders..."
        if code-insiders --install-extension "$VSIX_FILE" --force 2>/dev/null; then
            INSTALLED_EDITORS="${INSTALLED_EDITORS}VS Code Insiders, "
            echo "✓ Installed in VS Code Insiders"
        fi
    fi

    # Try Windsurf
    if command -v windsurf &>/dev/null; then
        echo "Installing extension in Windsurf..."
        if windsurf --install-extension "$VSIX_FILE" --force 2>/dev/null; then
            INSTALLED_EDITORS="${INSTALLED_EDITORS}Windsurf, "
            echo "✓ Installed in Windsurf"
        fi
    fi

    if [ -z "$INSTALLED_EDITORS" ]; then
        echo "⚠️  No editors found (code, cursor, code-insiders, windsurf)."
        echo "   Install manually: <editor> --install-extension $VSIX_FILE"
    fi
else
    echo ""
    echo "⚠️  Extension not available - hooks will still work in CLI mode."
    echo "   Download .vsix from GitHub releases or build: cd vscode-extension && npx @vscode/vsce package"
fi

echo ""
echo "=== Done! ==="
echo ""
echo "Installed:"
echo "  - StatusLine: context % with color coding (terminal)"
echo "  - Stop hook: warns at 40% context (edit compact-check.py to change)"
if [ -n "$INSTALLED_EDITORS" ]; then
    echo "  - Extension: dialog + auto /compact (${INSTALLED_EDITORS%, })"
fi
echo ""
echo "To customize thresholds, edit:"
echo "  $HOOKS_DIR/compact-check.py  (COMPACT_THRESHOLD_PCT)"
echo "  $HOOKS_DIR/context-monitor.js (WARN_PCT, DANGER_PCT)"
echo ""
echo "⚠️  Restart Claude Code and reload your editor for changes to take effect."
