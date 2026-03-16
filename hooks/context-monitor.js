#!/usr/bin/env node

// StatusLine hook: monitors context usage and writes metrics to a temp file.
// This is the ONLY hook type that receives live context_window data.
// Usage quota is fetched by compact-check.py (Stop hook) — we only read the cache here.

const fs = require('fs');
const path = require('path');
const os = require('os');
const { execSync } = require('child_process');

const BASE_DIR = process.env.COMPACT_GUARD_TMPDIR || os.tmpdir();
const METRICS_DIR = path.join(BASE_DIR, 'claude-code-compact-guard');
const USAGE_CACHE_FILE = path.join(METRICS_DIR, 'usage-cache.json');
const USAGE_CACHE_TTL_MS = 300000; // Consider cache valid for 300s

// Autocompact buffer (Claude Code reserves ~33K tokens for autocompact)
const AUTOCOMPACT_BUFFER_TOKENS = 33_000;

// Thresholds for status line color coding (absolute tokens, model-agnostic)
const WARN_TOKENS = 60_000;
const DANGER_TOKENS = 80_000;

function readUsageCache() {
  try {
    const cached = JSON.parse(fs.readFileSync(USAGE_CACHE_FILE, 'utf8'));
    if (Date.now() - cached._fetchedAt < USAGE_CACHE_TTL_MS) return cached;
  } catch {}
  return null;
}

let input = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', (chunk) => { input += chunk; });
process.stdin.on('end', () => {
  try {
    const data = JSON.parse(input);
    const ctx = data.context_window || {};
    const model = data.model || {};

    const cwd = data.cwd || '';
    const rawUsedPct = ctx.used_percentage ?? 0;
    const windowSize = ctx.context_window_size ?? 200000;
    const currentUsage = ctx.current_usage || {};

    // Recalculate percentage against effective window (excluding autocompact buffer)
    const effectiveWindow = windowSize - AUTOCOMPACT_BUFFER_TOKENS;
    const tokensUsed = Math.round((rawUsedPct / 100) * windowSize);
    const usedPct = Math.min(100, Math.round((tokensUsed / effectiveWindow) * 100));

    // Read usage quota from cache (written by compact-check.py Stop hook)
    const usage = readUsageCache();
    const fiveHour = usage?.five_hour;
    const sevenDay = usage?.seven_day;
    const sessionUsagePct = fiveHour ? Math.round(fiveHour.utilization) : null;
    const sessionResetsAt = fiveHour?.resets_at ?? null;
    const weeklyUsagePct = sevenDay ? Math.round(sevenDay.utilization) : null;

    // Write metrics for the Stop hook to read
    const metrics = {
      timestamp: Date.now(),
      used_percentage: usedPct,
      remaining_percentage: 100 - usedPct,
      context_window_size: effectiveWindow,
      total_input_tokens: ctx.total_input_tokens ?? 0,
      total_output_tokens: ctx.total_output_tokens ?? 0,
      cache_read_input_tokens: currentUsage.cache_read_input_tokens ?? 0,
      cache_creation_input_tokens: currentUsage.cache_creation_input_tokens ?? 0,
      session_usage_pct: sessionUsagePct,
      session_resets_at: sessionResetsAt,
      weekly_usage_pct: weeklyUsagePct,
      model_id: model.id ?? 'unknown',
      session_id: data.session_id ?? '',
      cwd: cwd,
    };

    // Write session-scoped metrics file so multiple sessions don't conflict
    try { fs.mkdirSync(METRICS_DIR, { recursive: true }); } catch { /* exists */ }
    const sessionId = (data.session_id ?? 'unknown').replace(/[/\\]/g, '').replace(/\.\./g, '');
    const sessionMetricsFile = path.join(METRICS_DIR, `metrics-${sessionId}.json`);

    // Only update last_interaction_time when token counts change (real model response)
    let lastInteractionTime = Date.now();
    try {
      const prev = JSON.parse(fs.readFileSync(sessionMetricsFile, 'utf8'));
      if (prev.last_interaction_time
          && prev.total_input_tokens === metrics.total_input_tokens
          && prev.total_output_tokens === metrics.total_output_tokens) {
        lastInteractionTime = prev.last_interaction_time;
      }
    } catch { /* no previous metrics */ }
    metrics.last_interaction_time = lastInteractionTime;

    fs.writeFileSync(sessionMetricsFile, JSON.stringify(metrics, null, 2));

    // Color-coded status line output (based on absolute token count, not %)
    const inputTokens = ctx.total_input_tokens ?? tokensUsed;
    let color;
    if (inputTokens >= DANGER_TOKENS) {
      color = '\x1b[38;5;208m'; // orange
    } else if (inputTokens >= WARN_TOKENS) {
      color = '\x1b[33m'; // yellow
    } else {
      color = '\x1b[32m'; // green
    }
    const reset = '\x1b[0m';
    const dimColor = '\x1b[38;5;238m';

    const modelName = model.display_name ?? 'Claude';

    // Get project name and git branch
    const project = cwd ? path.basename(cwd) : '';
    let branch = '';
    if (cwd) {
      try {
        branch = execSync('git --no-optional-locks branch --show-current', { cwd, encoding: 'utf8', stdio: ['pipe', 'pipe', 'pipe'] }).trim();
      } catch { /* not a git repo */ }
    }

    const tokensK = (tokensUsed / 1000).toFixed(0);
    const windowK = (effectiveWindow / 1000).toFixed(0);

    // Build graphical progress bar (10 segments)
    const barWidth = 10;
    let bar = '';
    for (let i = 0; i < barWidth; i++) {
      const segStart = i * 10;
      const progress = usedPct - segStart;
      if (progress >= 8) {
        bar += `${color}█${reset}`;
      } else if (progress >= 3) {
        bar += `${color}▄${reset}`;
      } else {
        bar += `${dimColor}░${reset}`;
      }
    }

    const lastActive = new Date(lastInteractionTime);
    const time = `${String(lastActive.getHours()).padStart(2, '0')}:${String(lastActive.getMinutes()).padStart(2, '0')}`;

    // Build output
    let output = `◆ ${modelName}`;
    if (project) output += ` │ ▪ ${project}`;
    if (branch) output += ` │ ⎇ ${branch}`;
    output += ` │ ◷ ${time}`;
    output += ` │ ${bar} ${usedPct}% (${tokensK}K/${windowK}K)`;
    if (sessionUsagePct != null) output += ` │ ⚡ ${sessionUsagePct}%`;

    process.stdout.write(output);
  } catch {
    process.stdout.write('Ctx: --');
  }
});
