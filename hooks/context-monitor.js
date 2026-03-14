#!/usr/bin/env node

// StatusLine hook: monitors context usage and writes metrics to a temp file.
// This is the ONLY hook type that receives live context_window data.

const fs = require('fs');
const path = require('path');
const os = require('os');

const METRICS_FILE = path.join(os.tmpdir(), 'claude-context-metrics.json');

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

    fs.writeFileSync(METRICS_FILE, JSON.stringify(metrics, null, 2));

    // Color-coded status line output
    let color;
    if (usedPct >= DANGER_PCT) {
      color = '\x1b[31m'; // red
    } else if (usedPct >= WARN_PCT) {
      color = '\x1b[33m'; // yellow
    } else {
      color = '\x1b[32m'; // green
    }
    const reset = '\x1b[0m';

    const modelName = model.display_name ?? 'Claude';
    const costStr = (cost.total_cost_usd ?? 0).toFixed(3);

    // Estimate tokens used (used_percentage * window_size / 100)
    const tokensUsed = Math.round((usedPct / 100) * windowSize);
    const tokensK = (tokensUsed / 1000).toFixed(0);
    const windowK = (windowSize / 1000).toFixed(0);

    process.stdout.write(
      `${modelName} | ${color}Ctx: ${usedPct}% (${tokensK}K/${windowK}K)${reset} | $${costStr}`
    );
  } catch {
    process.stdout.write('Ctx: --');
  }
});
