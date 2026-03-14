/**
 * Tests for the StatusLine hook (context-monitor.js) - metrics writing and output.
 */

const { describe, it, before, after } = require('node:test');
const assert = require('node:assert');
const { execFileSync } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');

const HOOK = path.join(__dirname, '..', 'hooks', 'context-monitor.js');

function runHook(input, tmpDir) {
    return execFileSync('node', [HOOK], {
        input: JSON.stringify(input),
        env: { ...process.env, COMPACT_GUARD_TMPDIR: tmpDir },
        encoding: 'utf8',
    });
}

function makeInput(overrides = {}) {
    return {
        session_id: 'test-sess',
        context_window: {
            used_percentage: 25,
            remaining_percentage: 75,
            context_window_size: 200000,
            total_input_tokens: 50000,
            total_output_tokens: 5000,
            current_usage: {
                cache_read_input_tokens: 40000,
                cache_creation_input_tokens: 10000,
            },
        },
        cost: { total_cost_usd: 0.123 },
        model: { id: 'claude-sonnet-4-6', display_name: 'Sonnet' },
        ...overrides,
    };
}

describe('metrics file writing', () => {
    let tmpDir;

    before(() => {
        tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'cg-test-'));
    });

    after(() => {
        fs.rmSync(tmpDir, { recursive: true, force: true });
    });

    it('writes session-scoped metrics file', () => {
        runHook(makeInput({ session_id: 'abc-123' }), tmpDir);

        const file = path.join(tmpDir, 'claude-compact-guard', 'metrics-abc-123.json');
        assert.ok(fs.existsSync(file));

        const metrics = JSON.parse(fs.readFileSync(file, 'utf8'));
        assert.strictEqual(metrics.used_percentage, 25);
        assert.strictEqual(metrics.session_id, 'abc-123');
        assert.strictEqual(metrics.context_window_size, 200000);
        assert.strictEqual(metrics.session_cost_usd, 0.123);
    });

    it('isolates sessions in separate files', () => {
        const inputA = makeInput({ session_id: 'sess-A' });
        const inputB = makeInput({
            session_id: 'sess-B',
            context_window: { ...makeInput().context_window, used_percentage: 70 },
        });

        runHook(inputA, tmpDir);
        runHook(inputB, tmpDir);

        const dir = path.join(tmpDir, 'claude-compact-guard');
        const metricsA = JSON.parse(fs.readFileSync(path.join(dir, 'metrics-sess-A.json'), 'utf8'));
        const metricsB = JSON.parse(fs.readFileSync(path.join(dir, 'metrics-sess-B.json'), 'utf8'));

        assert.strictEqual(metricsA.used_percentage, 25);
        assert.strictEqual(metricsB.used_percentage, 70);
    });

    it('uses "unknown" for missing session_id', () => {
        const input = makeInput();
        delete input.session_id;
        runHook(input, tmpDir);

        const file = path.join(tmpDir, 'claude-compact-guard', 'metrics-unknown.json');
        assert.ok(fs.existsSync(file));
    });
});


describe('status line output', () => {
    let tmpDir;

    before(() => {
        tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'cg-test-'));
    });

    after(() => {
        fs.rmSync(tmpDir, { recursive: true, force: true });
    });

    it('outputs green below 40%', () => {
        const output = runHook(makeInput(), tmpDir);
        assert.ok(output.includes('\x1b[32m'), 'expected green ANSI code');
        assert.ok(output.includes('25%'));
        assert.ok(output.includes('Sonnet'));
    });

    it('outputs yellow at 40-59%', () => {
        const input = makeInput({
            context_window: { ...makeInput().context_window, used_percentage: 45 },
        });
        const output = runHook(input, tmpDir);
        assert.ok(output.includes('\x1b[33m'), 'expected yellow ANSI code');
        assert.ok(output.includes('45%'));
    });

    it('outputs red at 60%+', () => {
        const input = makeInput({
            context_window: { ...makeInput().context_window, used_percentage: 75 },
        });
        const output = runHook(input, tmpDir);
        assert.ok(output.includes('\x1b[31m'), 'expected red ANSI code');
        assert.ok(output.includes('75%'));
    });

    it('includes cost in output', () => {
        const output = runHook(makeInput(), tmpDir);
        assert.ok(output.includes('$0.123'));
    });

    it('includes token counts', () => {
        const output = runHook(makeInput(), tmpDir);
        assert.ok(output.includes('50K/200K'));
    });

    it('handles invalid JSON gracefully', () => {
        const output = execFileSync('node', [HOOK], {
            input: 'not json',
            env: { ...process.env, COMPACT_GUARD_TMPDIR: tmpDir },
            encoding: 'utf8',
        });
        assert.strictEqual(output, 'Ctx: --');
    });
});
