import { NextResponse } from 'next/server';
import { pool } from '@/lib/db';

type Preset = Record<string, number | string[] | boolean | null>;

const SWING_PRESETS: Record<string, Preset> = {
  safe: {
    quant_score_min: 80, rsi_max: 25, sigma_min: 2.0,
    target_profit_pct: 2.0, stop_loss_pct: -3.0,
    min_net_margin_eur: 1.0, max_hold_days: 3,
    sectors_allowed: ['Healthcare', 'Consumer'],
    llm_strict: true,
  },
  balanced: {
    quant_score_min: 70, rsi_max: 30, sigma_min: 1.5,
    target_profit_pct: 3.0, stop_loss_pct: -5.0,
    min_net_margin_eur: 0.5, max_hold_days: 5,
    sectors_allowed: null,
    llm_strict: false,
  },
  aggressive: {
    quant_score_min: 60, rsi_max: 40, sigma_min: 1.0,
    target_profit_pct: 5.0, stop_loss_pct: -8.0,
    min_net_margin_eur: 0.25, max_hold_days: 10,
    sectors_allowed: null,
    llm_strict: false,
  },
};

const INTRADAY_PRESETS: Record<string, Preset> = {
  safe: {
    quant_score_min: 80, rsi_max: 20, sigma_min: 2.0,
    target_profit_pct: 0.5, stop_loss_pct: -0.7,
    min_net_margin_eur: 0.25, max_hold_days: 1,
    sectors_allowed: ['Healthcare', 'Consumer'],
    llm_strict: true,
  },
  balanced: {
    quant_score_min: 70, rsi_max: 25, sigma_min: 1.5,
    target_profit_pct: 1.0, stop_loss_pct: -1.2,
    min_net_margin_eur: 0.25, max_hold_days: 1,
    sectors_allowed: null,
    llm_strict: false,
  },
  aggressive: {
    quant_score_min: 60, rsi_max: 35, sigma_min: 1.0,
    target_profit_pct: 1.5, stop_loss_pct: -2.0,
    min_net_margin_eur: 0.25, max_hold_days: 1,
    sectors_allowed: null,
    llm_strict: false,
  },
};

export async function POST(req: Request) {
  const body = (await req.json().catch(() => ({}))) as { slot?: number; profile?: string };
  if (
    typeof body.slot !== 'number' ||
    !Number.isInteger(body.slot) ||
    body.slot < 1 || body.slot > 18 ||
    typeof body.profile !== 'string' ||
    !Object.prototype.hasOwnProperty.call(SWING_PRESETS, body.profile)
  ) {
    return NextResponse.json(
      { error: 'slot (1-18) and profile (safe|balanced|aggressive) required' },
      { status: 400 },
    );
  }

  const client = await pool.connect();
  try {
    const { rows } = await client.query<{ strategy: string }>(
      'SELECT strategy FROM slot_profiles WHERE slot=$1',
      [body.slot],
    );
    if (!rows[0]) {
      return NextResponse.json({ error: 'slot not found' }, { status: 404 });
    }
    const presets = rows[0].strategy === 'intraday' ? INTRADAY_PRESETS : SWING_PRESETS;
    const p = presets[body.profile];

    await client.query('BEGIN');
    try {
      await client.query(
        `UPDATE slot_profiles SET
           profile=$2, quant_score_min=$3, rsi_max=$4, sigma_min=$5,
           target_profit_pct=$6, stop_loss_pct=$7, min_net_margin_eur=$8,
           max_hold_days=$9, sectors_allowed=$10::jsonb, llm_strict=$11,
           updated_at=now()
         WHERE slot=$1`,
        [
          body.slot, body.profile,
          p.quant_score_min, p.rsi_max, p.sigma_min,
          p.target_profit_pct, p.stop_loss_pct, p.min_net_margin_eur,
          p.max_hold_days,
          p.sectors_allowed === null ? null : JSON.stringify(p.sectors_allowed),
          p.llm_strict,
        ],
      );
      await client.query(
        `INSERT INTO audit_log (actor, action, details)
         VALUES ($1, 'slot_profile_set', $2::jsonb)`,
        ['dashboard', JSON.stringify({ slot: body.slot, profile: body.profile })],
      );
      await client.query('COMMIT');
    } catch (e) {
      await client.query('ROLLBACK').catch(() => {});
      throw e;
    }
    return NextResponse.json({ ok: true, slot: body.slot, profile: body.profile });
  } finally {
    client.release();
  }
}
