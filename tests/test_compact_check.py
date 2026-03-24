"""Tests for the Stop hook (compact-check.py) decision logic."""

import json
import os
import subprocess
import sys
import time

HOOK = os.path.join(os.path.dirname(__file__), '..', 'hooks', 'compact-check.py')


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
