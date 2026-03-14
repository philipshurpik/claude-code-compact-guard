# Contributing to claude-code-compact-guard

Thanks for your interest in contributing!

## Quick start

1. Fork and clone the repo
2. Run `bash install.sh` to install locally
3. Make changes
4. Test manually with a Claude Code session

## Project structure

```
├── context-monitor.js       # StatusLine hook (Node.js)
├── compact-check.py         # Stop hook (Python 3.11+)
├── install.sh               # Installer script
└── vscode-extension/        # VS Code / Cursor extension source
    ├── extension.js
    └── package.json
```

## Testing

**Hooks** - simulate with mock data:

```bash
# Test StatusLine
echo '{"context_window":{"used_percentage":55,"remaining_percentage":45,"context_window_size":200000},"cost":{"total_cost_usd":0.1},"model":{"id":"claude-sonnet-4-6","display_name":"Sonnet"}}' | node context-monitor.js

# Test Stop hook (above threshold)
echo '{"used_percentage":45,"context_window_size":200000,"session_cost_usd":0.1}' > /tmp/claude-context-metrics.json
rm -f /tmp/claude-compact-cooldown
echo '{"stop_hook_active":false}' | python3 compact-check.py
```

**Extension** - build and install locally:

```bash
cd vscode-extension
npm install
npx @vscode/vsce package --allow-missing-repository
code --install-extension compact-guard-*.vsix --force
```

## Pull requests

- Keep it simple - this project is intentionally small
- Test with an actual Claude Code session before submitting
- Update the README if you change behavior or add config options

## Ideas for contribution

- Configurable threshold via environment variable (no file edit needed)
- Sound/audio alert option
- Auto-detect optimal threshold based on model context window size
- Better terminal detection for non-standard setups
- Support for JetBrains IDEs (IntelliJ, WebStorm)

## Releasing

Releases are automated via GitHub Actions. To publish:

1. Update version in `vscode-extension/package.json`
2. Commit and push
3. Create a git tag: `git tag v0.2.0 && git push --tags`
4. GitHub Actions builds the `.vsix` and creates a release with all assets
