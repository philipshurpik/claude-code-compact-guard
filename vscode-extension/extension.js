const vscode = require('vscode');
const fs = require('fs');
const path = require('path');
const os = require('os');

const BASE_DIR = process.env.COMPACT_GUARD_TMPDIR || os.tmpdir();
const TRIGGER_FILE = path.join(BASE_DIR, 'claude-code-compact-guard-trigger.json');
const METRICS_DIR = path.join(BASE_DIR, 'claude-code-compact-guard');
const HEARTBEAT_FILE = path.join(BASE_DIR, 'claude-code-compact-guard-active');

const COOLDOWN_MS = 200000; // Don't show compaction dialog more than once per 200s
const CACHE_TTL_SECONDS = 300; // Prompt cache expires after ~5 minutes of inactivity

let watcher = null;
let statusBarItem = null;
let debounceTimer = null;
let lastTriggerTime = 0;

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
            const ago = metrics.timestamp ? formatTimeAgo(metrics.timestamp) : '?';
            let sessionPart = '';
            if (metrics.session_usage_pct != null) {
                const resetsIn = formatResetsIn(metrics.session_resets_at);
                sessionPart = ` | Session: ${metrics.session_usage_pct}%${resetsIn ? ` (resets in ${resetsIn})` : ''}`;
            }
            const weeklyPart = metrics.weekly_usage_pct != null ? ` | Weekly: ${metrics.weekly_usage_pct}%` : '';
            vscode.window.showInformationMessage(
                `Claude Code: ${metrics.used_percentage}% (${usedK}K/${windowK}K) | ${ago}${sessionPart}${weeklyPart}`
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

    // Cooldown: don't nag more than once per COOLDOWN_MS
    const now = Date.now();
    if (now - lastTriggerTime < COOLDOWN_MS) return;
    lastTriggerTime = now;

    const pct = trigger.used_percentage || '?';
    const tokensK = trigger.tokens_used_k || '?';
    const windowK = trigger.window_k || '?';

    const message = `⚠️ Claude Code context at ${pct}% (${tokensK}K/${windowK}K tokens). Cache expires in ~5 min. Compact now?`;

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
    // Find a Claude Code terminal (strict match only — don't grab random shells)
    const claudeTerminal = findClaudeTerminal();

    if (claudeTerminal) {
        claudeTerminal.show();
        claudeTerminal.sendText('/compact', true);
        vscode.window.showInformationMessage('Compact Guard: sent /compact to Claude Code.');
        return;
    }

    // No terminal — focus Claude Code extension input and copy to clipboard
    vscode.env.clipboard.writeText('/compact');
    vscode.commands.executeCommand('claude-vscode.focus').then(
        () => vscode.window.showInformationMessage(
            'Compact Guard: Claude Code focused, /compact copied — paste (Cmd+V) and press Enter.'
        ),
        () => vscode.window.showWarningMessage(
            'Compact Guard: "/compact" copied to clipboard — paste it into Claude Code chat.'
        )
    );
}

function findClaudeTerminal() {
    // Only match terminals that are actually Claude Code, not random shells
    for (const terminal of vscode.window.terminals) {
        const name = terminal.name.toLowerCase();
        if (name.includes('claude')) return terminal;
    }
    return null;
}

function formatResetsIn(resetsAt) {
    if (!resetsAt) return null;
    const ms = new Date(resetsAt) - Date.now();
    if (ms <= 0) return null;
    const totalMin = Math.round(ms / 60000);
    const hr = Math.floor(totalMin / 60);
    const min = totalMin % 60;
    return hr > 0 ? `${hr}h ${min}m` : `${min}m`;
}

function formatTimeAgo(timestampMs) {
    const seconds = Math.round((Date.now() - timestampMs) / 1000);
    if (seconds < 60) return `${seconds}s ago`;
    const minutes = Math.round(seconds / 60);
    if (minutes < 60) return `${minutes}m ago`;
    const hours = Math.round(minutes / 60);
    return `${hours}h ago`;
}

function getWorkspacePaths() {
    return (vscode.workspace.workspaceFolders || []).map(f => f.uri.fsPath);
}

function readMetrics() {
    try {
        if (!fs.existsSync(METRICS_DIR)) return null;
        const files = fs.readdirSync(METRICS_DIR)
            .filter(f => f.startsWith('metrics-') && f.endsWith('.json'));
        if (files.length === 0) return null;

        const now = Date.now();
        const workspacePaths = getWorkspacePaths();

        // First pass: collect file info and clean up old files.
        // Only stat files here — don't read contents yet (avoids TOCTOU races).
        const candidates = [];
        for (const file of files) {
            const full = path.join(METRICS_DIR, file);
            let mtime;
            try { mtime = fs.statSync(full).mtimeMs; } catch { continue; }
            if (now - mtime > 3600000 * 24) {
                try { fs.unlinkSync(full); } catch { /* ignore */ }
                continue;
            }
            candidates.push({ full, mtime });
        }

        if (candidates.length === 0) return null;

        // Sort by mtime descending — most recent first
        candidates.sort((a, b) => b.mtime - a.mtime);

        // Pick the best file: prefer workspace match, fall back to most recent.
        // Read files lazily starting from newest until we find a workspace match or exhaust all.
        let fallback = null;
        for (const { full } of candidates) {
            let metrics;
            try { metrics = JSON.parse(fs.readFileSync(full, 'utf8')); } catch { continue; }
            if (!fallback) fallback = metrics;
            if (metrics.cwd && workspacePaths.some(wp => metrics.cwd === wp || metrics.cwd.startsWith(wp + path.sep))) {
                return metrics;
            }
        }

        return fallback;
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
    if (!metrics || metrics.used_percentage == null) {
        statusBarItem.hide();
        return;
    }

    const pct = metrics.used_percentage;

    let icon;
    if (pct >= 60) icon = '$(warning)';
    else if (pct >= 40) icon = '$(info)';
    else icon = '$(check)';

    // Cache countdown: time remaining until prompt cache expires
    let cachePart = '';
    if (metrics.last_interaction_time) {
        const elapsed = Math.floor((Date.now() - metrics.last_interaction_time) / 1000);
        const remaining = CACHE_TTL_SECONDS - elapsed;
        if (remaining > 0) {
            const m = Math.floor(remaining / 60);
            const s = remaining % 60;
            cachePart = ` | $${m}:${String(s).padStart(2, '0')}`;
        } else {
            cachePart = ' | $expired';
        }
    }

    const sessionPart = metrics.session_usage_pct != null ? ` | S: ${metrics.session_usage_pct}%` : '';
    statusBarItem.text = `${icon} Ctx: ${pct}%${cachePart}${sessionPart}`;

    const tooltipParts = [`Context: ${pct}%`];
    if (cachePart) tooltipParts.push(cachePart.includes('expired') ? 'Cache: expired' : `Cache: ${cachePart.slice(4)} remaining`);
    if (metrics.session_usage_pct != null) {
        const resetsIn = formatResetsIn(metrics.session_resets_at);
        tooltipParts.push(`Session: ${metrics.session_usage_pct}%${resetsIn ? ` (resets in ${resetsIn})` : ''}`);
    }
    if (metrics.weekly_usage_pct != null) tooltipParts.push(`Weekly: ${metrics.weekly_usage_pct}%`);
    statusBarItem.tooltip = tooltipParts.join(' | ');

    statusBarItem.show();
}

function deactivate() {
    if (watcher) watcher.close();
    removeHeartbeat();
}

module.exports = { activate, deactivate };
