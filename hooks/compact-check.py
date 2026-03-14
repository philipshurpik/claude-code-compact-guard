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

METRICS_FILE = os.path.join(tempfile.gettempdir(), 'claude-context-metrics.json')
COOLDOWN_FILE = os.path.join(tempfile.gettempdir(), 'claude-compact-cooldown')
TRIGGER_FILE = os.path.join(tempfile.gettempdir(), 'claude-compact-trigger.json')


def read_metrics() -> dict | None:
    try:
        with open(METRICS_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def is_in_cooldown() -> bool:
    try:
        mtime = os.path.getmtime(COOLDOWN_FILE)
        return (time.time() - mtime) < COOLDOWN_SECONDS
    except FileNotFoundError:
        return False


def set_cooldown():
    with open(COOLDOWN_FILE, 'w') as f:
        f.write('')


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

    # Prevent infinite loops - if we already blocked once, let Claude stop
    if input_data.get('stop_hook_active', False):
        sys.exit(0)

    metrics = read_metrics()
    if not metrics:
        sys.exit(0)

    used_pct = metrics.get('used_percentage', 0)
    if used_pct < COMPACT_THRESHOLD_PCT:
        sys.exit(0)

    if is_in_cooldown():
        sys.exit(0)

    set_cooldown()

    # Calculate useful stats for the message
    window_size = metrics.get('context_window_size', 200000)
    tokens_used_k = round((used_pct / 100) * window_size / 1000)
    window_k = round(window_size / 1000)
    cost = metrics.get('session_cost_usd', 0)

    # Trigger VS Code / Cursor extension dialog
    write_vscode_trigger(used_pct, tokens_used_k, window_k, cost)

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
