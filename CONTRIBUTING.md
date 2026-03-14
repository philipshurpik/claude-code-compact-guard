# Contributing to claude-code-compact-guard

Thanks for your interest in contributing!

## Quick start

1. Fork and clone the repo
2. Run `bash install.sh` to install locally
3. Make changes
4. Test manually with a Claude Code session

## Project structure

```
├── hooks/
│   ├── context-monitor.js   # StatusLine hook (Node.js)
│   └── compact-check.py     # Stop hook (Python 3.11+)
├── vscode-extension/        # VS Code / Cursor extension source
│   ├── extension.js
│   └── package.json
├── tests/
│   ├── test_compact_check.py      # Stop hook tests (pytest)
│   └── test_context_monitor.js    # StatusLine tests (node:test)
├── install.sh               # Installer script
└── .github/workflows/
    └── release.yml           # CI/CD
```

## Testing

**Automated tests** (run both from repo root):

```bash
# Stop hook tests (Python) - requires pytest
python3 -m pytest tests/test_compact_check.py -v

# StatusLine hook tests (Node.js) - no dependencies
node --test tests/test_context_monitor.js
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

## Releasing

Releases are automated via GitHub Actions. To publish:

1. Update version in `vscode-extension/package.json`
2. Commit and push
3. Create a git tag: `git tag v0.2.0 && git push --tags`
4. GitHub Actions builds the `.vsix` and creates a release with all assets
