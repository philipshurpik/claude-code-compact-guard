#!/usr/bin/env python3
"""Check context usage after each response and prompt compaction if threshold exceeded."""

import json
import os
import sys
import tempfile
import time

# --- Configuration ---
# Compact suggestion threshold (percentage of context window used).
# When context exceeds this, the hook blocks Claude and asks user to /compact.
COMPACT_THRESHOLD_PCT = 40

# Cooldown: don't nag more than once per N seconds
COOLDOWN_SECONDS = 120

_TMPDIR = os.environ.get('COMPACT_GUARD_TMPDIR', tempfile.gettempdir())
METRICS_DIR = os.path.join(_TMPDIR, 'claude-code-compact-guard')
TRIGGER_FILE = os.path.join(_TMPDIR, 'claude-code-compact-guard-trigger.json')
HEARTBEAT_FILE = os.path.join(_TMPDIR, 'claude-code-compact-guard-active')

HEARTBEAT_MAX_AGE_SECONDS = 30


def read_metrics(session_id: str) -> dict | None:
    """Read metrics for a specific session."""
    metrics_file = os.path.join(METRICS_DIR, f'metrics-{session_id}.json')
    try:
        with open(metrics_file) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def cooldown_file(session_id: str) -> str:
    return os.path.join(METRICS_DIR, f'cooldown-{session_id}')


def is_in_cooldown(session_id: str) -> bool:
    try:
        mtime = os.path.getmtime(cooldown_file(session_id))
        return (time.time() - mtime) < COOLDOWN_SECONDS
    except FileNotFoundError:
        return False


def set_cooldown(session_id: str):
    os.makedirs(METRICS_DIR, exist_ok=True)
    with open(cooldown_file(session_id), 'w') as f:
        f.write('')


def is_running_in_editor() -> bool:
    """Check if this Claude Code session is running inside VS Code / Cursor."""
    term = os.environ.get('TERM_PROGRAM', '').lower()
    return term in ('vscode', 'cursor')


def is_extension_active() -> bool:
    """Check if the VS Code / Cursor extension is running via heartbeat file."""
    try:
        mtime = os.path.getmtime(HEARTBEAT_FILE)
        return (time.time() - mtime) < HEARTBEAT_MAX_AGE_SECONDS
    except FileNotFoundError:
        return False


def should_extension_handle() -> bool:
    """Only let the extension handle if this session is inside the editor."""
    return is_running_in_editor() and is_extension_active()


def write_vscode_trigger(used_pct: int, tokens_used_k: int, window_k: int, cost: float):
    """Write trigger file for VS Code / Cursor extension dialog."""
    trigger = {
        'timestamp': int(time.time() * 1000),
        'used_percentage': used_pct,
        'tokens_used_k': tokens_used_k,
        'window_k': window_k,
        'session_cost_usd': cost,
    }
    try:
        with open(TRIGGER_FILE, 'w') as f:
            json.dump(trigger, f)
    except OSError:
        pass


def main():
    input_data = json.load(sys.stdin)

    if input_data.get('stop_hook_active', False):
        sys.exit(0)

    session_id = input_data.get('session_id', 'unknown')

    metrics = read_metrics(session_id)
    if not metrics:
        sys.exit(0)

    used_pct = metrics.get('used_percentage', 0)
    if used_pct < COMPACT_THRESHOLD_PCT:
        sys.exit(0)

    if is_in_cooldown(session_id):
        sys.exit(0)

    set_cooldown(session_id)

    # Calculate useful stats for the message
    window_size = metrics.get('context_window_size', 200000)
    tokens_used_k = round((used_pct / 100) * window_size / 1000)
    window_k = round(window_size / 1000)
    cost = metrics.get('session_cost_usd', 0)

    write_vscode_trigger(used_pct, tokens_used_k, window_k, cost)

    if should_extension_handle():
        # Session is inside VS Code/Cursor and extension is active.
        # Extension will show the dialog -- no need to block Claude.
        sys.exit(0)

    # No extension running - block Claude and ask it to warn the user (CLI fallback)
    reason = (
        f'⚠️ Context usage: {used_pct}% ({tokens_used_k}K/{window_k}K tokens, session cost: ${cost:.3f}). '
        f'Cache will expire in ~5 minutes. '
        f'Please tell the user: "Context is at {used_pct}% - I recommend running /compact now '
        f'to reduce costs. If you wait and the cache expires, the next message will send '
        f'{tokens_used_k}K tokens uncached, which is significantly more expensive." '
        f'Then wait for the user to decide.'
    )

    output = {'decision': 'block', 'reason': reason}
    print(json.dumps(output))
    sys.exit(0)


if __name__ == '__main__':
    main()
