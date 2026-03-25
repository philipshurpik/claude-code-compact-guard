const vscode = require('vscode');
const fs = require('fs');
const path = require('path');
const os = require('os');

const BASE_DIR = process.env.COMPACT_GUARD_TMPDIR || os.tmpdir();
const METRICS_DIR = path.join(BASE_DIR, 'claude-code-compact-guard');
const HEARTBEAT_FILE = path.join(BASE_DIR, 'claude-code-compact-guard-active');

const COOLDOWN_MS = 200000; // Don't show compaction dialog more than once per 200s
const CACHE_TTL_SECONDS = 240; // Prompt cache expires after ~4 minutes of inactivity
const CACHE_WARN_SECONDS = 90; // Show compact dialog when this much cache time remains
let statusBarItem = null;
let lastTriggerTime = 0;

function activate(context) {
    // Status bar item showing context %
    statusBarItem = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Right, 50);
    statusBarItem.command = 'compactGuard.showStatus';
    statusBarItem.tooltip = 'Claude Code context usage (Compact Guard)';
    context.subscriptions.push(statusBarItem);

    // Poll every 3s for heartbeat + status bar update
    const metricsInterval = setInterval(() => updateStatusBar(), 3000);
    context.subscriptions.push({ dispose: () => clearInterval(metricsInterval) });
    updateStatusBar();

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

function checkCacheExpiry(metrics) {
    if (!metrics.last_interaction_time) return;

    const level = metrics.level || 'ok';
    if (level !== 'danger') return;

    const elapsed = Math.floor((Date.now() - metrics.last_interaction_time) / 1000);
    const remaining = CACHE_TTL_SECONDS - elapsed;

    // Only trigger when cache is about to expire (within warn window) but not yet expired
    if (remaining > CACHE_WARN_SECONDS || remaining <= 0) return;

    // Cooldown: don't nag more than once per COOLDOWN_MS
    const now = Date.now();
    if (now - lastTriggerTime < COOLDOWN_MS) return;
    lastTriggerTime = now;

    const pct = metrics.used_percentage || '?';
    const tokensK = Math.round((metrics.total_input_tokens || 0) / 1000);
    const windowK = Math.round((metrics.context_window_size || 0) / 1000);

    vscode.window.showWarningMessage(
        `⚠️ Cache expires in ~${remaining}s — context at ${pct}% (${tokensK}K/${windowK}K). Compact now to save costs?`,
        { modal: false },
        'Run /compact',
        'Dismiss'
    ).then((choice) => {
        if (choice === 'Run /compact') {
            sendCompactToTerminal();
        }
    });
}

function updateStatusBar() {
    writeHeartbeat();

    const metrics = readMetrics();
    if (!metrics || metrics.used_percentage == null) {
        statusBarItem.hide();
        return;
    }

    const pct = metrics.used_percentage;

    const level = metrics.level || 'ok';
    const icon = level === 'danger' ? '$(error)' : level === 'warn' ? '$(warning)' : '$(check)';

    // Cache countdown: time remaining until prompt cache expires
    let cachePart = '';
    if (metrics.last_interaction_time) {
        const elapsed = Math.floor((Date.now() - metrics.last_interaction_time) / 1000);
        const remaining = CACHE_TTL_SECONDS - elapsed;
        if (remaining > 0) {
            // Round down to nearest 10s to avoid visual jitter (2:59→2:50, 2:07→2:00)
            const rounded = Math.floor(remaining / 10) * 10;
            const display = Math.max(rounded, 0);
            const m = Math.floor(display / 60);
            const s = display % 60;
            cachePart = ` | $(clock) ${m}:${String(s).padStart(2, '0')}`;
        } else {
            cachePart = ' | $(clock) expired';
        }
    }

    const sessionPart = metrics.session_usage_pct != null ? ` | $(graph-line) ${metrics.session_usage_pct}%` : '';
    statusBarItem.text = `$(dashboard) ${icon} ${pct}%${cachePart}${sessionPart}`;

    const usedK = Math.round((pct / 100) * (metrics.context_window_size || 0) / 1000);
    const windowK = Math.round((metrics.context_window_size || 0) / 1000);
    const tooltipParts = [`Context: ${pct}% (${usedK}K/${windowK}K)`];
    if (cachePart) tooltipParts.push(cachePart.includes('expired') ? 'Cache: expired' : `Cache: ${cachePart.slice(4)} remaining`);
    if (metrics.session_usage_pct != null) {
        const resetsIn = formatResetsIn(metrics.session_resets_at);
        tooltipParts.push(`Session: ${metrics.session_usage_pct}%${resetsIn ? ` (resets in ${resetsIn})` : ''}`);
    }
    if (metrics.weekly_usage_pct != null) tooltipParts.push(`Weekly: ${metrics.weekly_usage_pct}%`);
    statusBarItem.tooltip = tooltipParts.join(' | ');

    statusBarItem.show();

    // Check if cache is about to expire and context is high — prompt compact
    checkCacheExpiry(metrics);
}

function deactivate() {
    removeHeartbeat();
}

module.exports = { activate, deactivate };
