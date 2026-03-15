#!/usr/bin/env node

// StatusLine hook: monitors context usage and writes metrics to a temp file.
// This is the ONLY hook type that receives live context_window data.

const fs = require('fs');
const path = require('path');
const os = require('os');
const { execSync } = require('child_process');

const BASE_DIR = process.env.COMPACT_GUARD_TMPDIR || os.tmpdir();
const METRICS_DIR = path.join(BASE_DIR, 'claude-code-compact-guard');

// Thresholds for status line color coding
const WARN_PCT = 40;
const DANGER_PCT = 60;

let input = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', (chunk) => { input += chunk; });
process.stdin.on('end', () => {
  try {
    const data = JSON.parse(input);
    const ctx = data.context_window || {};
    const cost = data.cost || {};
    const model = data.model || {};

    const usedPct = ctx.used_percentage ?? 0;
    const remainingPct = ctx.remaining_percentage ?? 100;
    const windowSize = ctx.context_window_size ?? 200000;
    const currentUsage = ctx.current_usage || {};

    // Write metrics for the Stop hook to read
    const metrics = {
      timestamp: Date.now(),
      used_percentage: usedPct,
      remaining_percentage: remainingPct,
      context_window_size: windowSize,
      total_input_tokens: ctx.total_input_tokens ?? 0,
      total_output_tokens: ctx.total_output_tokens ?? 0,
      cache_read_input_tokens: currentUsage.cache_read_input_tokens ?? 0,
      cache_creation_input_tokens: currentUsage.cache_creation_input_tokens ?? 0,
      session_cost_usd: cost.total_cost_usd ?? 0,
      model_id: model.id ?? 'unknown',
      session_id: data.session_id ?? '',
    };

    // Write session-scoped metrics file so multiple sessions don't conflict
    try { fs.mkdirSync(METRICS_DIR, { recursive: true }); } catch { /* exists */ }
    const sessionId = data.session_id ?? 'unknown';
    const sessionMetricsFile = path.join(METRICS_DIR, `metrics-${sessionId}.json`);
    fs.writeFileSync(sessionMetricsFile, JSON.stringify(metrics, null, 2));

    // Color-coded status line output
    let color;
    if (usedPct >= DANGER_PCT) {
      color = '\x1b[38;5;208m'; // orange
    } else if (usedPct >= WARN_PCT) {
      color = '\x1b[33m'; // yellow
    } else {
      color = '\x1b[32m'; // green
    }
    const reset = '\x1b[0m';
    const dimColor = '\x1b[38;5;238m';

    const modelName = model.display_name ?? 'Claude';
    const cwd = data.cwd || '';

    // Get project name and git branch
    const project = cwd ? path.basename(cwd) : '';
    let branch = '';
    if (cwd) {
      try {
        branch = execSync('git branch --show-current', { cwd, encoding: 'utf8', stdio: ['pipe', 'pipe', 'pipe'] }).trim();
      } catch { /* not a git repo */ }
    }

    // Estimate tokens used (used_percentage * window_size / 100)
    const tokensUsed = Math.round((usedPct / 100) * windowSize);
    const tokensK = (tokensUsed / 1000).toFixed(0);
    const windowK = (windowSize / 1000).toFixed(0);

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

    // Build output
    let output = `🤖 ${modelName}`;
    if (project) output += ` | 📁 ${project}`;
    if (branch) output += ` | 🔀 ${branch}`;
    output += ` | ${bar} ${usedPct}% (${tokensK}K/${windowK}K)`;

    process.stdout.write(output);
  } catch {
    process.stdout.write('Ctx: --');
  }
});
