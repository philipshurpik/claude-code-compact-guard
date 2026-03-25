#!/usr/bin/env node

// StatusLine hook: monitors context usage and writes metrics to a temp file.

const fs = require('fs');
const path = require('path');
const os = require('os');
const { execSync } = require('child_process');

const BASE_DIR = process.env.COMPACT_GUARD_TMPDIR || os.tmpdir();
const METRICS_DIR = path.join(BASE_DIR, 'claude-code-compact-guard');

// Autocompact buffer (Claude Code reserves ~33K tokens for autocompact)
const AUTOCOMPACT_BUFFER_TOKENS = 33_000;

// Thresholds for status line color coding (absolute tokens, model-agnostic)
const WARN_TOKENS = 60_000;
const COMPACT_TOKENS = 80_000;

function formatReset(epoch) {
  if (!epoch) return '';
  const diff = Math.max(0, Math.round(epoch - Date.now() / 1000));
  if (diff <= 0) return 'now';
  if (diff < 60) return '<1m';
  const d = Math.floor(diff / 86400);
  const h = Math.floor((diff % 86400) / 3600);
  const m = Math.floor((diff % 3600) / 60);
  if (d > 0) return `${d}d${h}h`;
  if (h > 0) return `${h}h${m}m`;
  return `${m}m`;
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

    // Rate limits from Claude Code 2.1.80+
    const rateLimits = data.rate_limits || {};
    const rlFiveHour = rateLimits.five_hour;
    const rlSevenDay = rateLimits.seven_day;
    const sessionUsagePct = rlFiveHour?.used_percentage != null ? Math.round(rlFiveHour.used_percentage) : null;
    const sessionResetsAt = rlFiveHour?.resets_at ?? null;
    const weeklyUsagePct = rlSevenDay?.used_percentage != null ? Math.round(rlSevenDay.used_percentage) : null;
    const weeklyResetsAt = rlSevenDay?.resets_at ?? null;

    // Determine usage level based on absolute token thresholds (using tokensUsed which matches the displayed value)
    const level = tokensUsed >= COMPACT_TOKENS ? 'danger' : tokensUsed >= WARN_TOKENS ? 'warn' : 'ok';

    // Write metrics for the Stop hook to read
    const metrics = {
      timestamp: Date.now(),
      level,
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
      weekly_resets_at: weeklyResetsAt,
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

    // Color-coded status line output
    const color = level === 'danger' ? '\x1b[38;5;208m' : level === 'warn' ? '\x1b[33m' : '\x1b[32m';
    const reset = '\x1b[0m';
    const dimColor = '\x1b[38;5;238m';
    const mutedColor = '\x1b[38;5;245m';

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
    const bar = Array.from({ length: 10 }, (_, i) => {
      const progress = usedPct - i * 10;
      const ch = progress >= 8 ? '█' : progress >= 3 ? '▄' : null;
      return ch ? `${color}${ch}${reset}` : `${dimColor}░${reset}`;
    }).join('');

    const lastActive = new Date(lastInteractionTime);
    const time = `${String(lastActive.getHours()).padStart(2, '0')}:${String(lastActive.getMinutes()).padStart(2, '0')}`;

    // Build output
    let output = `◆ ${modelName}`;
    if (project) output += ` │ ▪ ${project}`;
    if (branch) output += ` │ ⎇ ${branch}`;
    output += ` │ ◷ ${time}`;
    output += ` │ ${bar} ${usedPct}% (${tokensK}K/${windowK}K)`;
    if (sessionUsagePct != null) {
      const rlColor = sessionUsagePct > 80 ? '\x1b[31m' : sessionUsagePct > 50 ? '\x1b[33m' : '\x1b[32m';
      const resetStr = formatReset(sessionResetsAt);
      output += ` │ ${rlColor}${sessionUsagePct}%${reset}`;
      if (resetStr) output += ` ${mutedColor}↻${resetStr}${reset}`;
    }
    if (weeklyUsagePct != null) {
      const rlColor = weeklyUsagePct > 80 ? '\x1b[31m' : weeklyUsagePct > 50 ? '\x1b[33m' : '\x1b[32m';
      const resetStr = formatReset(weeklyResetsAt);
      output += ` │ ${rlColor}${weeklyUsagePct}%${reset}`;
      if (resetStr) output += ` ${mutedColor}⟳${resetStr}${reset}`;
    }

    process.stdout.write(output);
  } catch {
    process.stdout.write('Ctx: --');
  }
});
