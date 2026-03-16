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
COOLDOWN_SECONDS = 200

_TMPDIR = os.environ.get('COMPACT_GUARD_TMPDIR', tempfile.gettempdir())
METRICS_DIR = os.path.join(_TMPDIR, 'claude-code-compact-guard')
TRIGGER_FILE = os.path.join(_TMPDIR, 'claude-code-compact-guard-trigger.json')
HEARTBEAT_FILE = os.path.join(_TMPDIR, 'claude-code-compact-guard-active')

HEARTBEAT_MAX_AGE_SECONDS = 30

# Claude Code reserves ~16.5% of context window for autocompact buffer
AUTOCOMPACT_BUFFER_RATIO = 0.165
CONTEXT_WINDOW_SIZE = 200000


def sanitize_session_id(session_id: str) -> str:
    return session_id.replace('/', '').replace('\\', '').replace('..', '')


def read_metrics(session_id: str) -> dict | None:
    """Read metrics for a specific session."""
    metrics_file = os.path.join(METRICS_DIR, f'metrics-{sanitize_session_id(session_id)}.json')
    try:
        with open(metrics_file) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def estimate_metrics_from_transcript(transcript_path: str, session_id: str, cwd: str = '') -> dict | None:
    """Estimate context metrics from transcript when StatusLine hook hasn't fired (VS Code mode)."""
    try:
        with open(transcript_path) as f:
            content = f.read().strip()
        if not content:
            return None

        if content.startswith('['):
            entries = json.loads(content)
        else:
            entries = [json.loads(line) for line in content.splitlines() if line.strip()]

        last_usage = None
        total_output = 0

        for entry in entries:
            if entry.get('type') == 'assistant' and 'message' in entry:
                msg = entry['message']
                if 'usage' in msg:
                    last_usage = msg['usage']
                    total_output += last_usage.get('output_tokens', 0)

        if not last_usage:
            return None

        input_tokens = last_usage.get('input_tokens', 0)
        cache_creation = last_usage.get('cache_creation_input_tokens', 0)
        cache_read = last_usage.get('cache_read_input_tokens', 0)
        total_input = input_tokens + cache_creation + cache_read

        effective_window = round(CONTEXT_WINDOW_SIZE * (1 - AUTOCOMPACT_BUFFER_RATIO))
        used_pct = min(100, round(total_input / effective_window * 100))

        return {
            'timestamp': int(time.time() * 1000),
            'used_percentage': used_pct,
            'remaining_percentage': 100 - used_pct,
            'context_window_size': effective_window,
            'total_input_tokens': total_input,
            'total_output_tokens': total_output,
            'cache_read_input_tokens': cache_read,
            'cache_creation_input_tokens': cache_creation,
            'session_id': session_id,
            'cwd': cwd,
        }
    except Exception:
        return None


def write_metrics(metrics: dict):
    """Write metrics file so the extension can read it."""
    os.makedirs(METRICS_DIR, exist_ok=True)
    metrics_file = os.path.join(METRICS_DIR, f'metrics-{sanitize_session_id(metrics["session_id"])}.json')
    try:
        with open(metrics_file, 'w') as f:
            json.dump(metrics, f, indent=2)
    except OSError:
        pass


def cooldown_file(session_id: str) -> str:
    return os.path.join(METRICS_DIR, f'cooldown-{sanitize_session_id(session_id)}')


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


def is_extension_active() -> bool:
    """Check if the VS Code / Cursor extension is running via heartbeat file."""
    try:
        mtime = os.path.getmtime(HEARTBEAT_FILE)
        return (time.time() - mtime) < HEARTBEAT_MAX_AGE_SECONDS
    except FileNotFoundError:
        return False


def should_extension_handle() -> bool:
    """Extension handles if it's alive — heartbeat is the reliable signal."""
    return is_extension_active()


def write_vscode_trigger(used_pct: int, tokens_used_k: int, window_k: int):
    """Write trigger file for VS Code / Cursor extension dialog."""
    trigger = {
        'timestamp': int(time.time() * 1000),
        'used_percentage': used_pct,
        'tokens_used_k': tokens_used_k,
        'window_k': window_k,
    }
    try:
        with open(TRIGGER_FILE, 'w') as f:
            json.dump(trigger, f)
    except OSError:
        pass


def main():
    try:
        input_data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        sys.exit(0)

    if input_data.get('stop_hook_active', False):
        sys.exit(0)

    session_id = input_data.get('session_id', 'unknown')
    cwd = input_data.get('cwd', '')

    # Prefer transcript estimation (always fresh) over cached metrics file.
    # StatusLine hook writes metrics in CLI mode, but doesn't fire in VS Code/Cursor.
    # Transcript is always up-to-date since Stop hook fires after every response.
    metrics = None
    transcript_path = input_data.get('transcript_path', '')
    if transcript_path:
        metrics = estimate_metrics_from_transcript(transcript_path, session_id, cwd)

    if not metrics:
        metrics = read_metrics(session_id)

    if not metrics:
        sys.exit(0)

    # Always write metrics so the extension can display context % in its status bar,
    # regardless of whether we're above the compact threshold.
    if cwd and not metrics.get('cwd'):
        metrics['cwd'] = cwd
    write_metrics(metrics)

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

    write_vscode_trigger(used_pct, tokens_used_k, window_k)

    if should_extension_handle():
        # Session is inside VS Code/Cursor and extension is active.
        # Extension will show the dialog -- no need to block Claude.
        sys.exit(0)

    # No extension running - block Claude and ask it to warn the user (CLI fallback)
    reason = (
        f'⚠️ Context usage: {used_pct}% ({tokens_used_k}K/{window_k}K tokens). '
        f'Cache will expire in ~5 minutes. '
        f'Please tell the user: "Context is at {used_pct}% - I recommend running /compact now '
        f'to save on costs. The prompt cache expires in ~5 min." '
        f'Then wait for the user to decide.'
    )

    output = {'decision': 'block', 'reason': reason}
    print(json.dumps(output))
    sys.exit(0)


if __name__ == '__main__':
    main()
