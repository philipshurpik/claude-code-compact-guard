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

        const file = path.join(tmpDir, 'claude-code-compact-guard', 'metrics-abc-123.json');
        assert.ok(fs.existsSync(file));

        const metrics = JSON.parse(fs.readFileSync(file, 'utf8'));
        assert.strictEqual(metrics.session_id, 'abc-123');
        // 25% of 200K raw = 50K tokens, effective window = 200K * 0.835 = 167K
        // usedPct = round(50000 / 167000 * 100) = 30
        assert.strictEqual(metrics.used_percentage, 30);
        assert.strictEqual(metrics.context_window_size, 167000);
    });

    it('isolates sessions in separate files', () => {
        const inputA = makeInput({ session_id: 'sess-A' });
        const inputB = makeInput({
            session_id: 'sess-B',
            context_window: { ...makeInput().context_window, used_percentage: 70 },
        });

        runHook(inputA, tmpDir);
        runHook(inputB, tmpDir);

        const dir = path.join(tmpDir, 'claude-code-compact-guard');
        const metricsA = JSON.parse(fs.readFileSync(path.join(dir, 'metrics-sess-A.json'), 'utf8'));
        const metricsB = JSON.parse(fs.readFileSync(path.join(dir, 'metrics-sess-B.json'), 'utf8'));

        // A: 25% raw -> 30% effective, B: 70% raw -> 84% effective
        assert.strictEqual(metricsA.used_percentage, 30);
        assert.strictEqual(metricsB.used_percentage, 84);
    });

    it('uses "unknown" for missing session_id', () => {
        const input = makeInput();
        delete input.session_id;
        runHook(input, tmpDir);

        const file = path.join(tmpDir, 'claude-code-compact-guard', 'metrics-unknown.json');
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

    it('outputs colored bar with model name', () => {
        const output = runHook(makeInput(), tmpDir);
        assert.ok(output.includes('Sonnet'), 'expected model name');
        assert.ok(output.includes('█'), 'expected filled bar segments');
        assert.ok(output.includes('░'), 'expected empty bar segments');
    });

    it('includes project and branch when cwd is a git repo', () => {
        const { execSync } = require('node:child_process');
        const input = makeInput({ cwd: process.cwd() });
        const output = runHook(input, tmpDir);
        const dirName = path.basename(process.cwd());
        assert.ok(output.includes(dirName), 'expected project name');
        // Branch symbol only appears when not on detached HEAD (CI uses detached checkout)
        let branch = '';
        try { branch = execSync('git branch --show-current', { encoding: 'utf8' }).trim(); } catch { /* ignore */ }
        if (branch) {
            assert.ok(output.includes('⎇'), 'expected branch symbol');
        }
    });

    it('omits project and branch when cwd is absent', () => {
        const output = runHook(makeInput(), tmpDir);
        assert.ok(!output.includes('▪'), 'no project without cwd');
        assert.ok(!output.includes('⎇'), 'no branch without cwd');
    });

    it('outputs yellow bar at warning level (60K-80K tokens)', () => {
        const input = makeInput({
            context_window: { ...makeInput().context_window, total_input_tokens: 65_000 },
        });
        const output = runHook(input, tmpDir);
        assert.ok(output.includes('\x1b[33m'), 'expected yellow ANSI code');
    });

    it('outputs orange bar at danger level (>=80K tokens)', () => {
        const input = makeInput({
            context_window: { ...makeInput().context_window, total_input_tokens: 85_000 },
        });
        const output = runHook(input, tmpDir);
        assert.ok(output.includes('\x1b[38;5;208m'), 'expected orange ANSI code');
    });

    it('includes token counts', () => {
        const output = runHook(makeInput(), tmpDir);
        // Effective window = 200K * 0.835 = 167K, tokens recalculated
        assert.ok(output.includes('K/167K'), 'expected effective window size');
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
