#!/usr/bin/env python3
"""Check context usage after each response and prompt compaction if threshold exceeded."""

import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.request

# --- Configuration ---
# Compact suggestion threshold in tokens (absolute, model-agnostic).
# When total input tokens exceed this, the hook blocks Claude and asks user to /compact.
COMPACT_THRESHOLD_TOKENS = 80_000

# Cooldown: don't nag more than once per N seconds
COOLDOWN_SECONDS = 200

_TMPDIR = os.environ.get('COMPACT_GUARD_TMPDIR', tempfile.gettempdir())
METRICS_DIR = os.path.join(_TMPDIR, 'claude-code-compact-guard')
TRIGGER_FILE = os.path.join(_TMPDIR, 'claude-code-compact-guard-trigger.json')
HEARTBEAT_FILE = os.path.join(_TMPDIR, 'claude-code-compact-guard-active')
USAGE_CACHE_FILE = os.path.join(METRICS_DIR, 'usage-cache.json')
USAGE_FETCH_LOCK_FILE = os.path.join(METRICS_DIR, '.usage-fetch-lock')

HEARTBEAT_MAX_AGE_SECONDS = 30


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
    """Extract token counts from transcript (VS Code mode — StatusLine hook doesn't fire there).

    Returns only raw token data; context_window_size and used_percentage are filled in main()
    from the cached metrics file written by context-monitor.js (which has the real window size
    from the live API — no model→size mapping needed here).
    """
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

        return {
            'timestamp': int(time.time() * 1000),
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


# --- Usage quota fetching (moved from context-monitor.js to avoid frequent API calls) ---
USAGE_CACHE_TTL = 300  # seconds
USAGE_FETCH_LOCK_TTL = 15  # seconds — max time to hold the fetch lock
OAUTH_CLIENT_ID = '9d1c250a-e61b-44d9-88ed-5944d1962f5e'
KEYCHAIN_SERVICE = 'Claude Code-credentials'


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


def _save_credentials(creds: dict):
    try:
        subprocess.check_call(
            [
                'security',
                'add-generic-password',
                '-U',
                '-s',
                KEYCHAIN_SERVICE,
                '-w',
                json.dumps(creds),
                '-a',
                'default',
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass


def _get_claude_code_version() -> str:
    try:
        out = subprocess.check_output(['claude', '--version'], stderr=subprocess.DEVNULL, text=True).strip()
        m = re.search(r'[\d.]+', out)
        return m.group(0) if m else 'unknown'
    except Exception:
        return 'unknown'


def _call_usage_api(token: str) -> dict | None:
    """Call the OAuth usage API. Returns parsed JSON on success, None on failure."""
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
            return json.loads(resp.read())
    except Exception:
        return None


def _refresh_oauth_token() -> str | None:
    """Refresh the OAuth token via console.anthropic.com. Returns new access token or None."""
    creds = _get_credentials()
    refresh_token = (creds or {}).get('claudeAiOauth', {}).get('refreshToken')
    if not refresh_token:
        return None
    post_data = json.dumps(
        {
            'grant_type': 'refresh_token',
            'refresh_token': refresh_token,
            'client_id': OAUTH_CLIENT_ID,
        }
    ).encode()
    req = urllib.request.Request(
        'https://console.anthropic.com/v1/oauth/token',
        data=post_data,
        headers={
            'Content-Type': 'application/json',
            'User-Agent': f'claude-code/{_get_claude_code_version()}',
        },
        method='POST',
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            tokens = json.loads(resp.read())
        if not tokens.get('access_token') or not tokens.get('refresh_token'):
            return None
        creds['claudeAiOauth']['accessToken'] = tokens['access_token']
        creds['claudeAiOauth']['refreshToken'] = tokens['refresh_token']
        if tokens.get('expires_in'):
            creds['claudeAiOauth']['expiresAt'] = int(time.time() * 1000) + tokens['expires_in'] * 1000
        _save_credentials(creds)
        return tokens['access_token']
    except Exception:
        return None


def _read_usage_cache(ignore_expiry: bool = False) -> dict | None:
    try:
        with open(USAGE_CACHE_FILE) as f:
            cached = json.load(f)
        if ignore_expiry or (time.time() - cached.get('_fetchedAt', 0) / 1000) < USAGE_CACHE_TTL:
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


def _acquire_fetch_lock() -> bool:
    """File-based lock ensuring only one process fetches usage at a time."""
    try:
        stat = os.stat(USAGE_FETCH_LOCK_FILE)
        if time.time() - stat.st_mtime < USAGE_FETCH_LOCK_TTL:
            return False
    except FileNotFoundError:
        pass
    try:
        os.makedirs(METRICS_DIR, exist_ok=True)
        with open(USAGE_FETCH_LOCK_FILE, 'w') as f:
            f.write(str(os.getpid()))
        return True
    except OSError:
        return False


def _release_fetch_lock():
    try:
        os.unlink(USAGE_FETCH_LOCK_FILE)
    except FileNotFoundError:
        pass


def fetch_session_usage() -> int | None:
    """Fetch session usage % from the OAuth API. Returns utilization or None."""
    cached = _read_usage_cache()
    if cached:
        five_hour = cached.get('five_hour')
        return round(five_hour['utilization']) if five_hour else None

    # Only one process should fetch at a time — others get stale cache or nothing
    if not _acquire_fetch_lock():
        stale = _read_usage_cache(ignore_expiry=True)
        if stale and stale.get('five_hour'):
            return round(stale['five_hour']['utilization'])
        return None

    try:
        token = (_get_credentials() or {}).get('claudeAiOauth', {}).get('accessToken')
        if not token:
            return None

        usage = _call_usage_api(token)

        # On failure (likely 429 / expired token), refresh and retry once
        if not usage:
            new_token = _refresh_oauth_token()
            if new_token:
                usage = _call_usage_api(new_token)

        if not usage:
            stale = _read_usage_cache(ignore_expiry=True)
            if stale and stale.get('five_hour'):
                return round(stale['five_hour']['utilization'])
            return None

        _write_usage_cache(usage)
        five_hour = usage.get('five_hour')
        return round(five_hour['utilization']) if five_hour else None
    finally:
        _release_fetch_lock()


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

    # Transcript gives fresh token counts; cached metrics (written by context-monitor.js) supply
    # the real context_window_size from the live API. Merge both when available.
    transcript_path = input_data.get('transcript_path', '')
    transcript_metrics = estimate_metrics_from_transcript(transcript_path, session_id, cwd) if transcript_path else None
    cached_metrics = read_metrics(session_id)

    if transcript_metrics:
        # Fill in window size + used_pct from cached metrics (context-monitor.js wrote the real value).
        window_size = (cached_metrics or {}).get('context_window_size', 0)
        transcript_metrics['context_window_size'] = window_size
        if window_size:
            total = transcript_metrics['total_input_tokens']
            transcript_metrics['used_percentage'] = min(100, round(total / window_size * 100))
            transcript_metrics['remaining_percentage'] = 100 - transcript_metrics['used_percentage']
        metrics = transcript_metrics
    elif cached_metrics:
        metrics = cached_metrics
    else:
        sys.exit(0)

    # Fetch session usage quota (once per 300s, locked to one process at a time)
    session_usage_pct = fetch_session_usage()
    if session_usage_pct is not None:
        metrics['session_usage_pct'] = session_usage_pct

    # Always write metrics so the extension can display context % in its status bar,
    # regardless of whether we're above the compact threshold.
    if cwd and not metrics.get('cwd'):
        metrics['cwd'] = cwd
    write_metrics(metrics)

    tokens_used = metrics.get('total_input_tokens', 0)
    if tokens_used < COMPACT_THRESHOLD_TOKENS:
        sys.exit(0)

    if is_in_cooldown(session_id):
        sys.exit(0)

    set_cooldown(session_id)

    # Calculate useful stats for the message
    used_pct = metrics.get('used_percentage', 0)
    window_size = metrics.get('context_window_size', 0)
    tokens_used_k = round(tokens_used / 1000)
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
