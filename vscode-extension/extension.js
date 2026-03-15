const vscode = require('vscode');
const fs = require('fs');
const path = require('path');
const os = require('os');

const BASE_DIR = process.env.COMPACT_GUARD_TMPDIR || os.tmpdir();
const TRIGGER_FILE = path.join(BASE_DIR, 'claude-code-compact-guard-trigger.json');
const METRICS_DIR = path.join(BASE_DIR, 'claude-code-compact-guard');
const HEARTBEAT_FILE = path.join(BASE_DIR, 'claude-code-compact-guard-active');

let watcher = null;
let statusBarItem = null;
let debounceTimer = null;

function activate(context) {
    // Status bar item showing context %
    statusBarItem = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Right, 50);
    statusBarItem.command = 'compactGuard.showStatus';
    statusBarItem.tooltip = 'Claude Code context usage (Compact Guard)';
    context.subscriptions.push(statusBarItem);

    // Start polling metrics for status bar (every 3s)
    const metricsInterval = setInterval(() => updateStatusBar(), 3000);
    context.subscriptions.push({ dispose: () => clearInterval(metricsInterval) });
    updateStatusBar();

    // Watch trigger file for compaction prompts
    ensureTriggerDir();
    startWatching(context);

    // Command: send /compact to Claude terminal
    context.subscriptions.push(
        vscode.commands.registerCommand('compactGuard.compactNow', () => {
            sendCompactToTerminal();
        })
    );

    // Command: show current status
    context.subscriptions.push(
        vscode.commands.registerCommand('compactGuard.showStatus', () => {
            const metrics = readMetrics();
            if (!metrics) {
                vscode.window.showInformationMessage('Compact Guard: No active Claude Code session detected.');
                return;
            }
            const windowK = Math.round(metrics.context_window_size / 1000);
            const usedK = Math.round((metrics.used_percentage / 100) * metrics.context_window_size / 1000);
            vscode.window.showInformationMessage(
                `Claude Code: ${metrics.used_percentage}% context used (${usedK}K/${windowK}K) | $${(metrics.session_cost_usd || 0).toFixed(3)} session cost`
            );
        })
    );
}

function ensureTriggerDir() {
    const dir = path.dirname(TRIGGER_FILE);
    if (!fs.existsSync(dir)) {
        fs.mkdirSync(dir, { recursive: true });
    }
}

function startWatching(context) {
    // Create trigger file if it doesn't exist so we can watch it
    if (!fs.existsSync(TRIGGER_FILE)) {
        fs.writeFileSync(TRIGGER_FILE, '{}');
    }

    try {
        watcher = fs.watch(TRIGGER_FILE, (eventType) => {
            if (eventType === 'change' || eventType === 'rename') {
                // Debounce: fs.watch can fire multiple events for a single write
                if (debounceTimer) clearTimeout(debounceTimer);
                debounceTimer = setTimeout(() => handleTrigger(), 300);
            }
        });
        context.subscriptions.push({ dispose: () => { if (watcher) watcher.close(); } });
    } catch {
        // If watch fails, fall back to polling
        const pollInterval = setInterval(() => {
            try {
                const stat = fs.statSync(TRIGGER_FILE);
                const age = Date.now() - stat.mtimeMs;
                if (age < 2000) handleTrigger();
            } catch { /* ignore */ }
        }, 2000);
        context.subscriptions.push({ dispose: () => clearInterval(pollInterval) });
    }
}

function handleTrigger() {
    let trigger;
    try {
        const raw = fs.readFileSync(TRIGGER_FILE, 'utf8').trim();
        if (!raw || raw === '{}') return;
        trigger = JSON.parse(raw);
    } catch {
        return;
    }

    // Only act on recent triggers (within 10 seconds)
    if (!trigger.timestamp || (Date.now() - trigger.timestamp) > 10000) return;

    // Clear trigger so we don't re-fire
    try { fs.writeFileSync(TRIGGER_FILE, '{}'); } catch { /* ignore */ }

    const pct = trigger.used_percentage || '?';
    const tokensK = trigger.tokens_used_k || '?';
    const windowK = trigger.window_k || '?';
    const cost = trigger.session_cost_usd != null ? `$${trigger.session_cost_usd.toFixed(3)}` : '';

    const message = [
        `⚠️ Claude Code context at ${pct}% (${tokensK}K/${windowK}K tokens).`,
        'Cache expires in ~5 min.',
        cost ? `Session cost: ${cost}.` : '',
        'Compact now to save on API costs?',
    ].filter(Boolean).join(' ');

    vscode.window.showWarningMessage(
        message,
        { modal: false },
        'Run /compact',
        'Dismiss'
    ).then((choice) => {
        if (choice === 'Run /compact') {
            sendCompactToTerminal();
        }
    });
}

function sendCompactToTerminal() {
    // Find a Claude Code terminal
    const claudeTerminal = findClaudeTerminal();

    if (claudeTerminal) {
        claudeTerminal.show();
        claudeTerminal.sendText('/compact', true);
        vscode.window.showInformationMessage('Compact Guard: sent /compact to Claude Code.');
    } else {
        // No Claude terminal found - copy to clipboard as fallback
        vscode.env.clipboard.writeText('/compact');
        vscode.window.showWarningMessage(
            'Compact Guard: no Claude Code terminal found. "/compact" copied to clipboard - paste it manually.'
        );
    }
}

function findClaudeTerminal() {
    const terminals = vscode.window.terminals;

    // Try exact matches first, then fuzzy
    const patterns = [
        (t) => t.name.toLowerCase().includes('claude'),
        (t) => t.name.toLowerCase().includes('cc'),
        // Last resort: use the active terminal if there's only one
        () => terminals.length === 1 ? terminals[0] : null,
    ];

    for (const match of patterns) {
        for (const terminal of terminals) {
            const result = match(terminal);
            if (result) return result;
        }
    }

    // Final fallback: active terminal
    return vscode.window.activeTerminal || null;
}

function readMetrics() {
    try {
        if (!fs.existsSync(METRICS_DIR)) return null;
        const files = fs.readdirSync(METRICS_DIR)
            .filter(f => f.startsWith('metrics-') && f.endsWith('.json'));
        if (files.length === 0) return null;

        // Pick the most recently modified metrics file
        let latest = null;
        let latestMtime = 0;
        for (const file of files) {
            const full = path.join(METRICS_DIR, file);
            const mtime = fs.statSync(full).mtimeMs;
            if (mtime > latestMtime) {
                latestMtime = mtime;
                latest = full;
            }
        }
        if (!latest) return null;
        return JSON.parse(fs.readFileSync(latest, 'utf8'));
    } catch {
        return null;
    }
}

function writeHeartbeat() {
    try {
        fs.writeFileSync(HEARTBEAT_FILE, String(Date.now()));
    } catch { /* ignore */ }
}

function removeHeartbeat() {
    try { fs.unlinkSync(HEARTBEAT_FILE); } catch { /* ignore */ }
}

function updateStatusBar() {
    writeHeartbeat();

    const metrics = readMetrics();
    if (!metrics || !metrics.used_percentage) {
        statusBarItem.hide();
        return;
    }

    const pct = metrics.used_percentage;
    let icon;
    if (pct >= 60) icon = '$(warning)';
    else if (pct >= 40) icon = '$(info)';
    else icon = '$(check)';

    statusBarItem.text = `${icon} Ctx: ${pct}%`;
    statusBarItem.show();
}

function deactivate() {
    if (watcher) watcher.close();
    removeHeartbeat();
}

module.exports = { activate, deactivate };
