import { Pool } from 'pg';

declare global {
  // eslint-disable-next-line no-var
  var __pgPool: Pool | undefined;
}

export const pool =
  global.__pgPool ??
  new Pool({
    connectionString: process.env.DATABASE_URL,
    max: 8,
  });

if (process.env.NODE_ENV !== 'production') global.__pgPool = pool;

export async function getConfig(): Promise<Record<string, unknown>> {
  const { rows } = await pool.query<{ key: string; value: unknown }>(
    'SELECT key, value FROM config',
  );
  return Object.fromEntries(rows.map((r) => [r.key, r.value]));
}

export async function getHeartbeat(component: string) {
  const { rows } = await pool.query(
    'SELECT ts, info FROM heartbeat WHERE component = $1',
    [component],
  );
  return rows[0] ?? null;
}

// Keys that the dashboard is allowed to write via setConfig. Anything not
// on this list must go through its own dedicated endpoint (e.g. BOT_ENABLED
// goes through /api/kill-switch, which calls setConfigTrusted). Guards the
// dashboard against "any key with a valid proxy secret" being writable.
const DASHBOARD_WRITABLE_KEYS = new Set<string>([
  'MANUAL_APPROVAL_MODE',
  'LLM_VETO_ENABLED',
  'LLM_SENTIMENT_SIZING_ENABLED',
  'LLM_TIER_SPLIT_ENABLED',
  'NEWS_WATCHER_ENABLED',
  'BRACKET_ORDER_ENABLED',
  'PARTIAL_TP_ENABLED',
  'VOLUME_HARD_GATE_ENABLED',
  'MULTI_TF_CONFIRM_ENABLED',
  'RSI_DIVERGENCE_ENABLED',
  'TRADING_MODE',
  'OVERNIGHT_ENABLED',
  'MIN_STOP_WIDTH_PCT',
  'APPROVAL_EXPIRY_SEC',
  'LLM_DAILY_BUDGET_USD',
  'LLM_HTTP_TIMEOUT_SEC',
  'MOC_WINDOW_MIN_MINUTES_USD', 'MOC_WINDOW_MAX_MINUTES_USD',
  'MOC_WINDOW_MIN_MINUTES_EU',  'MOC_WINDOW_MAX_MINUTES_EU',
]);

// Keys that ONLY dedicated endpoints may touch; setConfig refuses them even
// when the caller looks trusted (defence-in-depth against a leaked secret).
const DASHBOARD_FORBIDDEN_KEYS = new Set<string>([
  'BOT_ENABLED',
  'UNIVERSE',
  'OPTIMIZER_ENABLED',
]);

export class ConfigWriteDeniedError extends Error {
  constructor(key: string, reason: string) {
    super(`setConfig denied for "${key}": ${reason}`);
    this.name = 'ConfigWriteDeniedError';
  }
}

async function _writeConfig(
  key: string,
  value: unknown,
  actor: string,
): Promise<void> {
  const client = await pool.connect();
  try {
    await client.query('BEGIN');
    await client.query(
      `INSERT INTO config (key, value, updated_at, updated_by)
       VALUES ($1, $2::jsonb, now(), $3)
       ON CONFLICT (key) DO UPDATE
       SET value = EXCLUDED.value, updated_at = now(), updated_by = EXCLUDED.updated_by`,
      [key, JSON.stringify(value), actor],
    );
    await client.query(
      `INSERT INTO audit_log (actor, action, details)
       VALUES ($1, 'config_set', $2::jsonb)`,
      [actor, JSON.stringify({ key, value })],
    );
    await client.query('COMMIT');
  } catch (e) {
    await client.query('ROLLBACK').catch(() => {});
    throw e;
  } finally {
    client.release();
  }
}

export async function setConfig(
  key: string,
  value: unknown,
  actor: string,
): Promise<void> {
  if (DASHBOARD_FORBIDDEN_KEYS.has(key)) {
    throw new ConfigWriteDeniedError(key, 'dedicated endpoint required');
  }
  if (!DASHBOARD_WRITABLE_KEYS.has(key)) {
    throw new ConfigWriteDeniedError(key, 'not in dashboard whitelist');
  }
  await _writeConfig(key, value, actor);
}

// Trusted write path — called by dedicated endpoints (/api/kill-switch) that
// have their own auth/confirmation logic. Never expose this from a generic
// config-setter route.
export async function setConfigTrusted(
  key: string,
  value: unknown,
  actor: string,
): Promise<void> {
  await _writeConfig(key, value, actor);
}
