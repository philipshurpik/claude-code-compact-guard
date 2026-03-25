#!/usr/bin/env python3
"""Stop hook: writes metrics for the extension to read."""

import json
import os
import sys
import tempfile
import time

WARN_TOKENS = 60_000
COMPACT_TOKENS = 80_000
AUTOCOMPACT_BUFFER_TOKENS = 33_000

_TMPDIR = os.environ.get('COMPACT_GUARD_TMPDIR', tempfile.gettempdir())
METRICS_DIR = os.path.join(_TMPDIR, 'claude-code-compact-guard')


def infer_context_window(model_id: str) -> int:
    """Infer effective context window (minus autocompact buffer). [1m] suffix means 1M tokens, otherwise 200K."""
    raw = 1_000_000 if '[1m]' in model_id else 200_000
    return raw - AUTOCOMPACT_BUFFER_TOKENS


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
    """Extract token counts from transcript (VS Code mode — StatusLine hook doesn't fire there)."""
    try:
        with open(transcript_path) as f:
            content = f.read().strip()
        if not content:
            return None

        entries = (
            json.loads(content)
            if content.startswith('[')
            else [json.loads(line) for line in content.splitlines() if line.strip()]
        )

        last_usage = None
        model_id = ''
        total_output = 0

        for entry in entries:
            if entry.get('type') == 'assistant' and 'message' in entry:
                msg = entry['message']
                if 'usage' in msg:
                    last_usage = msg['usage']
                    total_output += last_usage.get('output_tokens', 0)
                if msg.get('model'):
                    model_id = msg['model']

        if not last_usage:
            return None

        input_tokens = last_usage.get('input_tokens', 0)
        cache_creation = last_usage.get('cache_creation_input_tokens', 0)
        cache_read = last_usage.get('cache_read_input_tokens', 0)
        total_input = input_tokens + cache_creation + cache_read

        window_size = infer_context_window(model_id) if model_id else 200_000 - AUTOCOMPACT_BUFFER_TOKENS
        used_pct = min(100, round(total_input / window_size * 100))
        level = 'danger' if total_input >= COMPACT_TOKENS else 'warn' if total_input >= WARN_TOKENS else 'ok'

        return {
            'timestamp': int(time.time() * 1000),
            'last_interaction_time': int(time.time() * 1000),
            'total_input_tokens': total_input,
            'total_output_tokens': total_output,
            'cache_read_input_tokens': cache_read,
            'cache_creation_input_tokens': cache_creation,
            'context_window_size': window_size,
            'used_percentage': used_pct,
            'remaining_percentage': 100 - used_pct,
            'level': level,
            'model_id': model_id,
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


def main():
    try:
        input_data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        sys.exit(0)

    session_id = input_data.get('session_id', 'unknown')
    cwd = input_data.get('cwd', '')

    # Transcript gives fresh token counts; cached metrics (written by context-monitor.js) supply
    # the real context_window_size from the live API. Merge both when available.
    transcript_path = input_data.get('transcript_path', '')
    transcript_metrics = estimate_metrics_from_transcript(transcript_path, session_id, cwd) if transcript_path else None
    cached_metrics = read_metrics(session_id)

    if transcript_metrics:
        # Merge rate-limit fields from cached metrics (context-monitor.js writes these).
        # Don't override context_window_size — transcript path already computes it correctly
        # via infer_context_window (with AUTOCOMPACT_BUFFER_TOKENS subtracted).
        cached = cached_metrics or {}
        for key in ('session_usage_pct', 'session_resets_at', 'weekly_usage_pct', 'weekly_resets_at'):
            if key in cached:
                transcript_metrics[key] = cached[key]
        metrics = transcript_metrics
    elif cached_metrics:
        metrics = cached_metrics
    else:
        sys.exit(0)

    if cwd and not metrics.get('cwd'):
        metrics['cwd'] = cwd
    write_metrics(metrics)


if __name__ == '__main__':
    main()
