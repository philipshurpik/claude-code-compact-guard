"""Tests for the Stop hook (compact-check.py) — writes metrics for the extension."""

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
    return subprocess.run(
        [sys.executable, HOOK],
        input=json.dumps(stdin_data),
        capture_output=True,
        text=True,
        env=env,
    )


def parse_output(result):
    if not result.stdout.strip():
        return None
    return json.loads(result.stdout)


def read_written_metrics(tmp_path, session_id):
    path = tmp_path / 'claude-code-compact-guard' / f'metrics-{session_id}.json'
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return None


class TestMetricsWriting:
    def test_writes_metrics_from_cached(self, tmp_path):
        write_metrics(tmp_path, 'sess-1', tokens=50_000)
        result = run_hook(tmp_path, {'session_id': 'sess-1'})
        assert result.returncode == 0
        assert parse_output(result) is None
        metrics = read_written_metrics(tmp_path, 'sess-1')
        assert metrics is not None
        assert metrics['total_input_tokens'] == 50_000

    def test_no_output_ever(self, tmp_path):
        """Stop hook never blocks Claude — only writes metrics."""
        write_metrics(tmp_path, 'sess-1', tokens=90_000)
        result = run_hook(tmp_path, {'session_id': 'sess-1'})
        assert result.returncode == 0
        assert parse_output(result) is None

    def test_stop_hook_active_exits_early(self, tmp_path):
        write_metrics(tmp_path, 'sess-1', tokens=90_000)
        result = run_hook(tmp_path, {'session_id': 'sess-1', 'stop_hook_active': True})
        assert parse_output(result) is None

    def test_no_metrics_exits_cleanly(self, tmp_path):
        result = run_hook(tmp_path, {'session_id': 'no-such-session'})
        assert result.returncode == 0
        assert parse_output(result) is None

    def test_adds_cwd_to_metrics(self, tmp_path):
        write_metrics(tmp_path, 'sess-1', tokens=50_000)
        run_hook(tmp_path, {'session_id': 'sess-1', 'cwd': '/some/path'})
        metrics = read_written_metrics(tmp_path, 'sess-1')
        assert metrics['cwd'] == '/some/path'

    def test_transcript_only_no_cached_metrics(self, tmp_path):
        """Without context-monitor.js, transcript alone produces full metrics with inferred window."""
        transcript = tmp_path / 'transcript.jsonl'
        entry = {
            'type': 'assistant',
            'message': {
                'model': 'claude-sonnet-4-6',
                'usage': {
                    'input_tokens': 60_000,
                    'output_tokens': 5_000,
                    'cache_creation_input_tokens': 0,
                    'cache_read_input_tokens': 0,
                },
            },
        }
        transcript.write_text(json.dumps(entry))

        run_hook(tmp_path, {'session_id': 'sess-1', 'transcript_path': str(transcript)})

        metrics = read_written_metrics(tmp_path, 'sess-1')
        assert metrics is not None
        assert metrics['total_input_tokens'] == 60_000
        assert metrics['context_window_size'] == 167_000  # inferred: 200K - 33K autocompact buffer
        assert metrics['used_percentage'] == 36  # 60K / 167K
        assert metrics['level'] == 'warn'  # 60K >= WARN_TOKENS
        assert metrics['model_id'] == 'claude-sonnet-4-6'
        assert metrics['last_interaction_time'] is not None

    def test_transcript_1m_model_infers_large_window(self, tmp_path):
        """Model with [1m] suffix gets 1M context window."""
        transcript = tmp_path / 'transcript.jsonl'
        entry = {
            'type': 'assistant',
            'message': {
                'model': 'claude-opus-4-6[1m]',
                'usage': {
                    'input_tokens': 100_000,
                    'output_tokens': 5_000,
                    'cache_creation_input_tokens': 0,
                    'cache_read_input_tokens': 0,
                },
            },
        }
        transcript.write_text(json.dumps(entry))

        run_hook(tmp_path, {'session_id': 'sess-1', 'transcript_path': str(transcript)})

        metrics = read_written_metrics(tmp_path, 'sess-1')
        assert metrics['context_window_size'] == 967_000  # 1M - 33K autocompact buffer
        assert metrics['used_percentage'] == 10  # 100K / 967K

    def test_preserves_cached_fields_with_transcript(self, tmp_path):
        """When cached metrics exist (from context-monitor.js), they override inferred values."""
        metrics_dir = tmp_path / 'claude-code-compact-guard'
        metrics_dir.mkdir(exist_ok=True)
        cached = {
            'timestamp': int(time.time() * 1000),
            'context_window_size': 167_000,
            'total_input_tokens': 50_000,
            'session_id': 'sess-1',
            'level': 'warn',
            'last_interaction_time': 1234567890,
            'model_id': 'claude-sonnet-4-6',
        }
        (metrics_dir / 'metrics-sess-1.json').write_text(json.dumps(cached))

        transcript = tmp_path / 'transcript.jsonl'
        entry = {
            'type': 'assistant',
            'message': {
                'model': 'claude-sonnet-4-6',
                'usage': {
                    'input_tokens': 60_000,
                    'output_tokens': 5_000,
                    'cache_creation_input_tokens': 0,
                    'cache_read_input_tokens': 0,
                },
            },
        }
        transcript.write_text(json.dumps(entry))

        run_hook(tmp_path, {'session_id': 'sess-1', 'transcript_path': str(transcript)})

        metrics = read_written_metrics(tmp_path, 'sess-1')
        assert metrics['total_input_tokens'] == 60_000
        # Transcript infers window: 200K - 33K = 167K (same as cached in this case)
        assert metrics['context_window_size'] == 167_000
        assert metrics['used_percentage'] == 36  # 60K / 167K
        # Transcript sets last_interaction_time to current time, not cached value
        assert abs(metrics['last_interaction_time'] - time.time() * 1000) < 5000


class TestSessionIsolation:
    def test_reads_own_session_metrics(self, tmp_path):
        write_metrics(tmp_path, 'sess-low', tokens=50_000)
        write_metrics(tmp_path, 'sess-high', tokens=90_000)

        run_hook(tmp_path, {'session_id': 'sess-low'})
        run_hook(tmp_path, {'session_id': 'sess-high'})

        low = read_written_metrics(tmp_path, 'sess-low')
        high = read_written_metrics(tmp_path, 'sess-high')
        assert low['total_input_tokens'] == 50_000
        assert high['total_input_tokens'] == 90_000

    def test_missing_session_metrics_no_write(self, tmp_path):
        write_metrics(tmp_path, 'other-session', tokens=90_000)
        run_hook(tmp_path, {'session_id': 'my-session'})
        assert read_written_metrics(tmp_path, 'my-session') is None
