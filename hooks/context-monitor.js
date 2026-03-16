#!/usr/bin/env node

// StatusLine hook: monitors context usage and writes metrics to a temp file.
// This is the ONLY hook type that receives live context_window data.

const fs = require('fs');
const path = require('path');
const os = require('os');
const https = require('https');
const { execSync, execFileSync } = require('child_process');

const BASE_DIR = process.env.COMPACT_GUARD_TMPDIR || os.tmpdir();
const METRICS_DIR = path.join(BASE_DIR, 'claude-code-compact-guard');
const USAGE_CACHE_FILE = path.join(METRICS_DIR, 'usage-cache.json');
const USAGE_CACHE_TTL_MS = 300000; // Cache usage data for 300 sec to avoid 429s

// Autocompact buffer ratio (Claude Code reserves ~16.5% for autocompact)
const AUTOCOMPACT_BUFFER_RATIO = 0.165;

// Thresholds for status line color coding (against effective/usable window)
const WARN_PCT = 40;
const DANGER_PCT = 60;

const OAUTH_CLIENT_ID = '9d1c250a-e61b-44d9-88ed-5944d1962f5e';
// Public OAuth client ID for Claude Code (used for usage quota API)
const KEYCHAIN_SERVICE = 'Claude Code-credentials';

function getClaudeCodeVersion() {
  try {
    return execSync('claude --version', { encoding: 'utf8', stdio: ['pipe', 'pipe', 'pipe'] })
      .trim().match(/[\d.]+/)?.[0] || 'unknown';
  } catch { return 'unknown'; }
}

function getCredentials() {
  try {
    const raw = execSync(
      `security find-generic-password -s "${KEYCHAIN_SERVICE}" -w`,
      { encoding: 'utf8', stdio: ['pipe', 'pipe', 'pipe'] }
    ).trim();
    return JSON.parse(raw);
  } catch { return null; }
}

function getOAuthToken() {
  return getCredentials()?.claudeAiOauth?.accessToken || null;
}

function saveCredentials(creds) {
  const json = JSON.stringify(creds);
  execFileSync(
    'security',
    ['add-generic-password', '-U', '-s', KEYCHAIN_SERVICE, '-w', json, '-a', 'default'],
    { stdio: ['pipe', 'pipe', 'pipe'] }
  );
}

function refreshOAuthToken() {
  return new Promise((resolve) => {
    const creds = getCredentials();
    const refreshToken = creds?.claudeAiOauth?.refreshToken;
    if (!refreshToken) { resolve(null); return; }

    const postData = JSON.stringify({
      grant_type: 'refresh_token',
      refresh_token: refreshToken,
      client_id: OAUTH_CLIENT_ID,
    });

    const req = https.request('https://console.anthropic.com/v1/oauth/token', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(postData) },
      timeout: 5000,
    }, (res) => {
      let body = '';
      res.on('data', (c) => { body += c; });
      res.on('end', () => {
        try {
          if (res.statusCode !== 200) { resolve(null); return; }
          const tokens = JSON.parse(body);
          if (!tokens.access_token || !tokens.refresh_token) { resolve(null); return; }
          // Write new tokens back to Keychain so Claude Code stays in sync
          creds.claudeAiOauth.accessToken = tokens.access_token;
          creds.claudeAiOauth.refreshToken = tokens.refresh_token;
          if (tokens.expires_in) {
            creds.claudeAiOauth.expiresAt = Date.now() + tokens.expires_in * 1000;
          }
          saveCredentials(creds);
          resolve(tokens.access_token);
        } catch { resolve(null); }
      });
    });
    req.on('error', () => resolve(null));
    req.on('timeout', () => { req.destroy(); resolve(null); });
    req.write(postData);
    req.end();
  });
}

function readUsageCache(ignoreExpiry = false) {
  try {
    const cached = JSON.parse(fs.readFileSync(USAGE_CACHE_FILE, 'utf8'));
    if (ignoreExpiry || Date.now() - cached._fetchedAt < USAGE_CACHE_TTL_MS) return cached;
  } catch {}
  return null;
}

function callUsageApi(token) {
  return new Promise((resolve) => {
    const req = https.get('https://api.anthropic.com/api/oauth/usage', {
      headers: {
        'Authorization': `Bearer ${token}`,
        'anthropic-beta': 'oauth-2025-04-20',
        'User-Agent': `claude-code/${getClaudeCodeVersion()}`,
        'Accept': 'application/json',
      },
      timeout: 3000,
    }, (res) => {
      let body = '';
      res.on('data', (c) => { body += c; });
      res.on('end', () => resolve({ statusCode: res.statusCode, body }));
    });
    req.on('error', () => resolve(null));
    req.on('timeout', () => { req.destroy(); resolve(null); });
  });
}

async function fetchUsage() {
  const cached = readUsageCache();
  if (cached) return cached;

  const token = getOAuthToken();
  if (!token) return null;

  let result = await callUsageApi(token);

  // On 429, refresh the token and retry once
  if (result?.statusCode === 429) {
    const newToken = await refreshOAuthToken();
    if (newToken) result = await callUsageApi(newToken);
  }

  if (!result || result.statusCode !== 200) return readUsageCache(true) || null;

  try {
    const usage = JSON.parse(result.body);
    usage._fetchedAt = Date.now();
    try { fs.writeFileSync(USAGE_CACHE_FILE, JSON.stringify(usage, null, 2)); } catch {}
    return usage;
  } catch { return readUsageCache(true) || null; }
}

let input = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', (chunk) => { input += chunk; });
process.stdin.on('end', async () => {
  try {
    const data = JSON.parse(input);
    const ctx = data.context_window || {};
    const model = data.model || {};

    const rawUsedPct = ctx.used_percentage ?? 0;
    const windowSize = ctx.context_window_size ?? 200000;
    const currentUsage = ctx.current_usage || {};

    // Recalculate percentage against effective window (excluding autocompact buffer)
    const effectiveWindow = Math.round(windowSize * (1 - AUTOCOMPACT_BUFFER_RATIO));
    const tokensUsed = Math.round((rawUsedPct / 100) * windowSize);
    const usedPct = Math.min(100, Math.round((tokensUsed / effectiveWindow) * 100));

    // Fetch usage quota (cached, non-blocking)
    const usage = await fetchUsage();
    const fiveHour = usage?.five_hour;
    const sessionUsagePct = fiveHour ? Math.round(fiveHour.utilization) : null;

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

    const now = new Date();
    const time = `${String(now.getHours()).padStart(2, '0')}:${String(now.getMinutes()).padStart(2, '0')}`;

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
