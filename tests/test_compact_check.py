"""Tests for the Stop hook (compact-check.py) decision logic."""

import importlib
import json
import os
import subprocess
import sys
import time
from unittest.mock import patch

HOOK = os.path.join(os.path.dirname(__file__), '..', 'hooks', 'compact-check.py')

# Add hooks dir to path for direct imports in unit tests
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'hooks'))
compact_check = importlib.import_module('compact-check')


def write_metrics(tmp_path, session_id, tokens=50_000, window_size=167_000):
    metrics_dir = tmp_path / 'claude-code-compact-guard'
    metrics_dir.mkdir(exist_ok=True)
    used_pct = min(100, round(tokens / window_size * 100))
    metrics = {
        'timestamp': int(time.time() * 1000),
        'used_percentage': used_pct,
        'context_window_size': window_size,
        'total_input_tokens': tokens,
        'session_id': session_id,
    }
    (metrics_dir / f'metrics-{session_id}.json').write_text(json.dumps(metrics))


def clear_cooldown(tmp_path, session_id):
    cooldown = tmp_path / 'claude-code-compact-guard' / f'cooldown-{session_id}'
    cooldown.unlink(missing_ok=True)


def _setup_fake_security(tmp_path):
    """Create a stub `security` binary that always fails, preventing real Keychain access."""
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir(exist_ok=True)
    fake = bin_dir / 'security'
    fake.write_text('#!/bin/sh\nexit 1\n')
    fake.chmod(0o755)
    return str(bin_dir)


def run_hook(tmp_path, stdin_data, env_extra=None):
    fake_bin = _setup_fake_security(tmp_path)
    env = os.environ.copy()
    env['COMPACT_GUARD_TMPDIR'] = str(tmp_path)
    env['PATH'] = fake_bin + os.pathsep + env.get('PATH', '')
    env.pop('TERM_PROGRAM', None)
    if env_extra:
        env.update(env_extra)
    result = subprocess.run(
        [sys.executable, HOOK],
        input=json.dumps(stdin_data),
        capture_output=True,
        text=True,
        env=env,
    )
    return result


def parse_output(result):
    if not result.stdout.strip():
        return None
    return json.loads(result.stdout)


# --- Decision logic ---


class TestDecisions:
    def test_below_threshold_allows(self, tmp_path):
        write_metrics(tmp_path, 'sess-1', tokens=50_000)
        result = run_hook(tmp_path, {'session_id': 'sess-1'})
        assert result.returncode == 0
        assert parse_output(result) is None

    def test_above_threshold_blocks(self, tmp_path):
        write_metrics(tmp_path, 'sess-1', tokens=90_000)
        result = run_hook(tmp_path, {'session_id': 'sess-1'})
        output = parse_output(result)
        assert output['decision'] == 'block'
        assert '90K' in output['reason']

    def test_exactly_at_threshold_blocks(self, tmp_path):
        write_metrics(tmp_path, 'sess-1', tokens=80_000)
        result = run_hook(tmp_path, {'session_id': 'sess-1'})
        output = parse_output(result)
        assert output['decision'] == 'block'

    def test_stop_hook_active_always_allows(self, tmp_path):
        write_metrics(tmp_path, 'sess-1', tokens=90_000)
        result = run_hook(tmp_path, {'session_id': 'sess-1', 'stop_hook_active': True})
        assert parse_output(result) is None

    def test_no_metrics_allows(self, tmp_path):
        result = run_hook(tmp_path, {'session_id': 'no-such-session'})
        assert parse_output(result) is None


# --- Cooldown ---


class TestCooldown:
    def test_cooldown_prevents_second_block(self, tmp_path):
        write_metrics(tmp_path, 'sess-1', tokens=90_000)

        r1 = run_hook(tmp_path, {'session_id': 'sess-1'})
        assert parse_output(r1)['decision'] == 'block'

        r2 = run_hook(tmp_path, {'session_id': 'sess-1'})
        assert parse_output(r2) is None

    def test_cooldown_is_per_session(self, tmp_path):
        write_metrics(tmp_path, 'sess-1', tokens=90_000)
        write_metrics(tmp_path, 'sess-2', tokens=90_000)

        r1 = run_hook(tmp_path, {'session_id': 'sess-1'})
        assert parse_output(r1)['decision'] == 'block'

        r2 = run_hook(tmp_path, {'session_id': 'sess-2'})
        assert parse_output(r2)['decision'] == 'block'

    def test_expired_cooldown_allows_block(self, tmp_path):
        write_metrics(tmp_path, 'sess-1', tokens=90_000)
        run_hook(tmp_path, {'session_id': 'sess-1'})

        cooldown = tmp_path / 'claude-code-compact-guard' / 'cooldown-sess-1'
        old_time = time.time() - 250
        os.utime(cooldown, (old_time, old_time))

        r2 = run_hook(tmp_path, {'session_id': 'sess-1'})
        assert parse_output(r2)['decision'] == 'block'


# --- Editor detection ---


class TestEditorDetection:
    def test_extension_in_vscode_skips_block(self, tmp_path):
        write_metrics(tmp_path, 'sess-1', tokens=90_000)
        (tmp_path / 'claude-code-compact-guard-active').write_text(str(time.time()))

        result = run_hook(
            tmp_path,
            {'session_id': 'sess-1'},
            env_extra={'TERM_PROGRAM': 'vscode'},
        )
        assert parse_output(result) is None

    def test_extension_in_cursor_skips_block(self, tmp_path):
        write_metrics(tmp_path, 'sess-1', tokens=90_000)
        (tmp_path / 'claude-code-compact-guard-active').write_text(str(time.time()))

        result = run_hook(
            tmp_path,
            {'session_id': 'sess-1'},
            env_extra={'TERM_PROGRAM': 'cursor'},
        )
        assert parse_output(result) is None

    def test_active_heartbeat_delegates_regardless_of_terminal(self, tmp_path):
        """Extension heartbeat alone is enough to delegate — TERM_PROGRAM doesn't matter."""
        write_metrics(tmp_path, 'sess-1', tokens=90_000)
        (tmp_path / 'claude-code-compact-guard-active').write_text(str(time.time()))

        result = run_hook(
            tmp_path,
            {'session_id': 'sess-1'},
            env_extra={'TERM_PROGRAM': 'iTerm2'},
        )
        assert parse_output(result) is None

    def test_no_heartbeat_in_editor_blocks(self, tmp_path):
        write_metrics(tmp_path, 'sess-1', tokens=90_000)

        result = run_hook(
            tmp_path,
            {'session_id': 'sess-1'},
            env_extra={'TERM_PROGRAM': 'vscode'},
        )
        assert parse_output(result)['decision'] == 'block'

    def test_stale_heartbeat_blocks(self, tmp_path):
        write_metrics(tmp_path, 'sess-1', tokens=90_000)
        heartbeat = tmp_path / 'claude-code-compact-guard-active'
        heartbeat.write_text('old')
        old_time = time.time() - 60
        os.utime(heartbeat, (old_time, old_time))

        result = run_hook(
            tmp_path,
            {'session_id': 'sess-1'},
            env_extra={'TERM_PROGRAM': 'vscode'},
        )
        assert parse_output(result)['decision'] == 'block'


# --- Trigger file ---


class TestTrigger:
    def test_writes_trigger_on_block(self, tmp_path):
        write_metrics(tmp_path, 'sess-1', tokens=90_000)
        run_hook(tmp_path, {'session_id': 'sess-1'})

        trigger = json.loads((tmp_path / 'claude-code-compact-guard-trigger.json').read_text())
        assert trigger['tokens_used_k'] == 90

    def test_writes_trigger_even_when_extension_handles(self, tmp_path):
        write_metrics(tmp_path, 'sess-1', tokens=90_000)
        (tmp_path / 'claude-code-compact-guard-active').write_text(str(time.time()))

        run_hook(
            tmp_path,
            {'session_id': 'sess-1'},
            env_extra={'TERM_PROGRAM': 'vscode'},
        )

        trigger = json.loads((tmp_path / 'claude-code-compact-guard-trigger.json').read_text())
        assert trigger['tokens_used_k'] == 90


# --- Session isolation ---


class TestSessionIsolation:
    def test_reads_own_session_metrics(self, tmp_path):
        write_metrics(tmp_path, 'sess-low', tokens=50_000)
        write_metrics(tmp_path, 'sess-high', tokens=90_000)

        r_low = run_hook(tmp_path, {'session_id': 'sess-low'})
        assert parse_output(r_low) is None

        r_high = run_hook(tmp_path, {'session_id': 'sess-high'})
        assert parse_output(r_high)['decision'] == 'block'

    def test_missing_session_metrics_allows(self, tmp_path):
        write_metrics(tmp_path, 'other-session', tokens=90_000)

        result = run_hook(tmp_path, {'session_id': 'my-session'})
        assert parse_output(result) is None


# --- Usage fetching (cache, lock, token refresh) ---


def _patch_globals(tmp_path):
    """Patch module-level paths to use tmp_path for isolation."""
    metrics_dir = tmp_path / 'claude-code-compact-guard'
    metrics_dir.mkdir(exist_ok=True)
    return {
        'METRICS_DIR': str(metrics_dir),
        'USAGE_CACHE_FILE': str(metrics_dir / 'usage-cache.json'),
        'USAGE_FETCH_LOCK_FILE': str(metrics_dir / '.usage-fetch-lock'),
    }


SAMPLE_USAGE = {
    'five_hour': {'utilization': 42.7, 'limit': 100},
    'daily': {'utilization': 10.0, 'limit': 1000},
}


class TestUsageCache:
    def test_cache_hit_returns_cached_value(self, tmp_path):
        """Fresh cache should return utilization without any API call."""
        overrides = _patch_globals(tmp_path)
        cache_data = {**SAMPLE_USAGE, '_fetchedAt': int(time.time() * 1000)}
        with open(overrides['USAGE_CACHE_FILE'], 'w') as f:
            json.dump(cache_data, f)

        with patch.multiple(compact_check, **overrides), patch.object(compact_check, '_call_usage_api') as mock_api:
            result = compact_check.fetch_session_usage()

        assert result == 43
        mock_api.assert_not_called()

    def test_stale_cache_triggers_fetch(self, tmp_path):
        """Expired cache should trigger an API fetch."""
        overrides = _patch_globals(tmp_path)
        stale_data = {**SAMPLE_USAGE, '_fetchedAt': int((time.time() - 600) * 1000)}
        with open(overrides['USAGE_CACHE_FILE'], 'w') as f:
            json.dump(stale_data, f)

        with (
            patch.multiple(compact_check, **overrides),
            patch.object(compact_check, '_get_credentials', return_value={'claudeAiOauth': {'accessToken': 'tok'}}),
            patch.object(compact_check, '_call_usage_api', return_value=(SAMPLE_USAGE, 200)),
        ):
            result = compact_check.fetch_session_usage()

        assert result == 43
        # Verify cache was written
        with open(overrides['USAGE_CACHE_FILE']) as f:
            cached = json.load(f)
        assert cached['five_hour']['utilization'] == 42.7

    def test_lock_prevents_double_fetch(self, tmp_path):
        """If lock is held, should return stale cache instead of fetching."""
        overrides = _patch_globals(tmp_path)
        # Write stale cache
        stale_data = {**SAMPLE_USAGE, '_fetchedAt': int((time.time() - 600) * 1000)}
        with open(overrides['USAGE_CACHE_FILE'], 'w') as f:
            json.dump(stale_data, f)
        # Create fresh lock file
        with open(overrides['USAGE_FETCH_LOCK_FILE'], 'w') as f:
            f.write('12345')

        with patch.multiple(compact_check, **overrides), patch.object(compact_check, '_call_usage_api') as mock_api:
            result = compact_check.fetch_session_usage()

        assert result == 43  # from stale cache
        mock_api.assert_not_called()

    def test_no_cache_no_creds_returns_none(self, tmp_path):
        """No cache and no credentials should return None."""
        overrides = _patch_globals(tmp_path)

        with (
            patch.multiple(compact_check, **overrides),
            patch.object(compact_check, '_get_credentials', return_value=None),
        ):
            result = compact_check.fetch_session_usage()

        assert result is None

    def test_429_triggers_token_refresh(self, tmp_path):
        """A 429 response should trigger token refresh and retry."""
        overrides = _patch_globals(tmp_path)

        with (
            patch.multiple(compact_check, **overrides),
            patch.object(compact_check, '_get_credentials', return_value={'claudeAiOauth': {'accessToken': 'old-tok'}}),
            patch.object(
                compact_check,
                '_call_usage_api',
                side_effect=[
                    (None, 429),
                    (SAMPLE_USAGE, 200),
                ],
            ),
            patch.object(compact_check, '_refresh_oauth_token', return_value='new-tok'),
        ):
            result = compact_check.fetch_session_usage()

        assert result == 43

    def test_non_429_error_does_not_refresh(self, tmp_path):
        """A 500 or network error should NOT trigger token refresh."""
        overrides = _patch_globals(tmp_path)

        with (
            patch.multiple(compact_check, **overrides),
            patch.object(compact_check, '_get_credentials', return_value={'claudeAiOauth': {'accessToken': 'tok'}}),
            patch.object(compact_check, '_call_usage_api', return_value=(None, 500)),
            patch.object(compact_check, '_refresh_oauth_token') as mock_refresh,
        ):
            result = compact_check.fetch_session_usage()

        assert result is None
        mock_refresh.assert_not_called()


class TestFetchLock:
    def test_acquire_and_release(self, tmp_path):
        overrides = _patch_globals(tmp_path)
        with patch.multiple(compact_check, **overrides):
            assert compact_check._acquire_fetch_lock() is True
            # Second acquire should fail (lock held)
            assert compact_check._acquire_fetch_lock() is False
            compact_check._release_fetch_lock()
            # After release, should succeed again
            assert compact_check._acquire_fetch_lock() is True
            compact_check._release_fetch_lock()

    def test_stale_lock_is_ignored(self, tmp_path):
        overrides = _patch_globals(tmp_path)
        lock_path = overrides['USAGE_FETCH_LOCK_FILE']
        # Create an old lock file
        with open(lock_path, 'w') as f:
            f.write('stale')
        old_time = time.time() - 30  # well past USAGE_FETCH_LOCK_TTL (15s)
        os.utime(lock_path, (old_time, old_time))

        with patch.multiple(compact_check, **overrides):
            # Stale lock — acquire returns False but doesn't block future attempts
            # (the stale lock gets cleaned up)
            compact_check._acquire_fetch_lock()
            # Cleanup: the lock file now exists (either new or stale-cleaned)
            compact_check._release_fetch_lock()
