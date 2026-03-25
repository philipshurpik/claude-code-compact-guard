#!/usr/bin/env python3
"""Stop hook: writes metrics for the extension to read."""

import functools
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

WARN_TOKENS = 60_000
COMPACT_TOKENS = 80_000
AUTOCOMPACT_BUFFER_TOKENS = 33_000
USAGE_CACHE_TTL = 300

_TMPDIR = os.environ.get('COMPACT_GUARD_TMPDIR', tempfile.gettempdir())
METRICS_DIR = os.path.join(_TMPDIR, 'claude-code-compact-guard')
USAGE_CACHE_FILE = os.path.join(METRICS_DIR, 'usage-cache.json')

OAUTH_CLIENT_ID = '9d1c250a-e61b-44d9-88ed-5944d1962f5e'
KEYCHAIN_SERVICE = 'Claude Code-credentials'


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


def _get_credentials() -> dict | None:
    try:
        raw = subprocess.check_output(
            ['security', 'find-generic-password', '-s', KEYCHAIN_SERVICE, '-w'],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
        return json.loads(raw)
    except Exception:
        return None


@functools.lru_cache(maxsize=1)
def _get_claude_code_version() -> str:
    try:
        out = subprocess.check_output(['claude', '--version'], stderr=subprocess.DEVNULL, text=True).strip()
        m = re.search(r'[\d.]+', out)
        return m.group(0) if m else 'unknown'
    except Exception:
        return 'unknown'


def _read_usage_cache() -> dict | None:
    try:
        with open(USAGE_CACHE_FILE) as f:
            cached = json.load(f)
        if (time.time() - cached.get('_fetchedAt', 0) / 1000) < USAGE_CACHE_TTL:
            return cached
    except Exception:
        pass
    return None


def _write_usage_cache(usage: dict):
    os.makedirs(METRICS_DIR, exist_ok=True)
    usage['_fetchedAt'] = int(time.time() * 1000)
    try:
        with open(USAGE_CACHE_FILE, 'w') as f:
            json.dump(usage, f, indent=2)
        os.chmod(USAGE_CACHE_FILE, 0o600)
    except OSError:
        pass


def fetch_usage() -> dict | None:
    """Fetch session/weekly usage from OAuth API with 300s caching. Returns parsed API response or None."""
    cached = _read_usage_cache()
    if cached:
        return cached

    token = (_get_credentials() or {}).get('claudeAiOauth', {}).get('accessToken')
    if not token:
        return None

    req = urllib.request.Request(
        'https://api.anthropic.com/api/oauth/usage',
        headers={
            'Authorization': f'Bearer {token}',
            'anthropic-beta': 'oauth-2025-04-20',
            'User-Agent': f'claude-code/{_get_claude_code_version()}',
            'Accept': 'application/json',
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            usage = json.loads(resp.read())
        _write_usage_cache(usage)
        return usage
    except Exception:
        return None


def _extract_usage_metrics(usage: dict) -> dict:
    """Extract session and weekly usage percentages and reset times from API response."""
    result = {}
    five_hour = usage.get('five_hour')
    if five_hour:
        result['session_usage_pct'] = round(five_hour['utilization'])
        if five_hour.get('resets_at'):
            result['session_resets_at'] = five_hour['resets_at']
    weekly = usage.get('weekly')
    if weekly:
        result['weekly_usage_pct'] = round(weekly['utilization'])
        if weekly.get('resets_at'):
            result['weekly_resets_at'] = weekly['resets_at']
    return result


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

    # Fetch session/weekly usage from OAuth API (cached for 300s)
    usage = fetch_usage()
    if usage:
        metrics.update(_extract_usage_metrics(usage))

    if cwd and not metrics.get('cwd'):
        metrics['cwd'] = cwd
    write_metrics(metrics)


if __name__ == '__main__':
    main()
