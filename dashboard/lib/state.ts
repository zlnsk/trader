import { pool } from './db';

export type Money = number;

export type PriceTick = { ts: string; price: number };

export type Position = {
  id: number;
  symbol: string;
  companyName: string | null;
  sector: string | null;
  slot: number;
  status: string;
  entry: Money;
  current: Money;
  target: Money;
  stop: Money;
  qty: number;
  unrealizedEur: Money;          // gross: (current - entry) * qty
  unrealizedPct: number;         // gross % (matches unrealizedEur)
  feesPaidEur: Money;            // fees booked so far on this position (entry side)
  unrealizedNetEur: Money;       // unrealizedEur - estimated round-trip fees (2 × feesPaidEur)
  unrealizedNetPct: number;      // unrealizedNetEur / (entry * qty) as %
  heldDays: number;
  progressPct: number;
  priceHistory: PriceTick[];
};

export type Signal = {
  id: number;
  ts: string;
  symbol: string;
  decision: string;
  quantScore: number | null;
  rsi: number | null;
  llmVerdict: string | null;
  reasoning: string | null;
};

export type Trade = {
  symbol: string;
  entry: Money;
  exit: Money;          // current_price when isOpen=true, exit_price otherwise
  openedAt: string;     // ISO timestamp of position open
  closedAt: string | null; // ISO timestamp of position close, null while open
  heldDays: number;
  fees: Money;
  netEur: Money;
  isOpen: boolean;      // true = position still open, values are live/unrealized
};

export type Stats = {
  portfolioEur: Money;
  portfolioChangeEur: Money;
  portfolioChangePct: number;
  deployedEur: Money;
  slotsUsed: number;
  maxSlots: number;
  monthPnl: Money;
  monthTrades: number;
  monthWins: number;
  monthLosses: number;
  allTimePnl: Money;
  allTimeTrades: number;
  allTimeWinRatePct: number;
};

export type StatusInfo = {
  botEnabled: boolean;
  ibConnected: boolean;
  mode: string;
  universeSize: number;
  lastSignalAgeSec: number | null;
  heartbeatAgeSec: number | null;
  manualApprovalMode: boolean;
  newsWatcherEnabled: boolean;
};

export type Regime = {
  regime: string;
  confidence: number | null;
  reasoning: string | null;
  ageSec: number | null;
};

export type SlotProfile = {
  slot: number;
  profile: 'safe' | 'balanced' | 'aggressive';
  strategy: 'swing' | 'intraday';
  quantScoreMin: number;
  rsiMax: number;
  sigmaMin: number;
  targetProfitPct: number;
  stopLossPct: number;
  minNetMarginEur: number;
  maxHoldSeconds: number;
  sectorsAllowed: string[] | null;
  llmStrict: boolean;
};

export type DailyReport = {
  date: string;
  summary: string | null;
  wins: number;
  losses: number;
  netPnl: number;
  recommendations: Array<{ change: string; why: string }> | null;
};

export type TuningProposal = {
  id: number;
  ts: string;
  status: string;
  proposals: Array<{ key: string; from: number; to: number; why: string }>;
  overallRationale: string | null;
};

export type PendingApproval = {
  id: number;
  ts: string;
  symbol: string;
  slot: number;
  strategy: string;
  profile: string;
  quantScore: number | null;
  rsi: number | null;
  llmVerdict: string | null;
  reasoning: string | null;
  price: number;
  qty: number;
  target: number;
  stop: number;
  currency: string;
};

export type Briefing = {
  id: number;
  ts: string;
  kind: string;
  region: string | null;
  summary: string | null;
  candidates: Array<{ symbol: string; why: string }> | null;
};

export type TargetProgress = {
  annualTargetPct: number;
  realizedPnl: number;
  unrealizedPnl: number;
  totalPnl: number;
  deployedEur: number;
  annualisedPct: number | null;
};



export type TodayOrder = {
  id: number;
  ts: string;
  symbol: string | null;
  side: 'BUY' | 'SELL';
  status: string;
  limitPrice: number | null;
  fillPrice: number | null;
  fillQty: number | null;
  slot: number | null;
  positionLive: boolean;  // position is currently open (opening/open/closing)
};


export type FillQuality = {
  sampleCount: number;
  avgSpreadBps: number | null;
  avgSlippageBps: number | null;
  paperOptimismEur: number; // sum of (fill - shadow) * qty across filled orders
};

export type State = {
  status: StatusInfo;
  stats: Stats;
  regime: Regime | null;
  regimeCrypto: Regime | null;
  slotProfiles: SlotProfile[];
  positions: Position[];
  latestSignal: Signal | null;
  recentTrades: Trade[];
  latestReport: DailyReport | null;
  pendingProposals: TuningProposal[];
  pendingApprovals: PendingApproval[];
  latestBriefing: Briefing | null;
  targetProgress: TargetProgress;
  todayOrders: TodayOrder[];
  fillQuality: FillQuality;
};

type AccountTag = { value: string; currency: string };
type HeartbeatInfo = {
  bot_enabled?: boolean;
  ib_connected?: boolean;
  mode?: string;
  universe_size?: number;
  account?: Record<string, AccountTag>;
};

function toNum(v: unknown): number {
  if (v === null || v === undefined) return 0;
  if (typeof v === 'number') return v;
  if (typeof v === 'string') return Number(v);
  return 0;
}

function accountNum(account: Record<string, AccountTag> | undefined, tag: string): number {
  const v = account?.[tag]?.value;
  return v ? Number(v) : 0;
}

export async function computeState(): Promise<State> {
  const [configR, hbR, posR, sigR, tradeR, allTimeR, monthR, lastSigR,
         regimeR, slotsR, reportR, proposalsR, approvalsR, briefingR,
         openedAtStartR, regimeCryptoR, todayOrdersR, fillQualityR, todayLiveR] = await Promise.all([
    pool.query<{ key: string; value: unknown }>('SELECT key, value FROM config'),
    pool.query<{ ts: Date; info: HeartbeatInfo }>(
      "SELECT ts, info FROM heartbeat WHERE component='bot'",
    ),
    pool.query(
      `SELECT id, symbol, slot, status, entry_price, qty, target_price, stop_price,
              current_price, opened_at, sector, company_name
       FROM positions WHERE status IN ('opening','open','closing')
       ORDER BY slot`,
    ),
    pool.query(
      `(SELECT id, ts, symbol, decision, quant_score, payload, llm_verdict, reason
          FROM signals WHERE decision <> 'skip' OR quant_score >= 70
          ORDER BY ts DESC LIMIT 1)
       UNION ALL
       (SELECT id, ts, symbol, decision, quant_score, payload, llm_verdict, reason
          FROM signals ORDER BY ts DESC LIMIT 1)
       LIMIT 1`,
    ),
    pool.query(
      `
       -- Recent trades: open positions from today (shown with live current_price as exit)
       -- unioned with last 48h closed positions. Order by most-recent activity.
       (SELECT p.symbol, p.entry_price,
               p.current_price AS exit_price,
               p.opened_at,
               NOW() AS closed_at,
               COALESCE(f.fees, 0) AS fees,
               (p.current_price - p.entry_price) * p.qty - COALESCE(f.fees, 0) AS net_eur,
               TRUE AS is_open
          FROM positions p
          LEFT JOIN (SELECT position_id, SUM(fees) AS fees FROM orders GROUP BY position_id) f
                 ON f.position_id = p.id
         WHERE p.status IN ('open','opening','closing')
           AND p.opened_at::date = CURRENT_DATE)
       UNION ALL
       (SELECT p.symbol, p.entry_price, p.exit_price, p.opened_at, p.closed_at,
               COALESCE(f.fees, 0) AS fees,
               (p.exit_price - p.entry_price) * p.qty - COALESCE(f.fees, 0) AS net_eur,
               FALSE AS is_open
          FROM positions p
          LEFT JOIN (SELECT position_id, SUM(fees) AS fees FROM orders GROUP BY position_id) f
                 ON f.position_id = p.id
         WHERE p.status = 'closed'
           AND p.closed_at > NOW() - interval '48 hours')
       ORDER BY closed_at DESC
       LIMIT 12
      `,
    ),
    pool.query<{ count: string; wins: string; total: string }>(
      `SELECT COUNT(*) AS count,
              COUNT(*) FILTER (WHERE p.exit_price > p.entry_price) AS wins,
              COALESCE(SUM((p.exit_price - p.entry_price) * p.qty - COALESCE(f.fees, 0)), 0) AS total
       FROM positions p
       LEFT JOIN (SELECT position_id, SUM(fees) AS fees FROM orders GROUP BY position_id) f
              ON f.position_id = p.id
       WHERE p.status = 'closed'`,
    ),
    pool.query<{ count: string; wins: string; losses: string; total: string }>(
      `SELECT COUNT(*) AS count,
              COUNT(*) FILTER (WHERE p.exit_price > p.entry_price) AS wins,
              COUNT(*) FILTER (WHERE p.exit_price <= p.entry_price) AS losses,
              COALESCE(SUM((p.exit_price - p.entry_price) * p.qty - COALESCE(f.fees, 0)), 0) AS total
       FROM positions p
       LEFT JOIN (SELECT position_id, SUM(fees) AS fees FROM orders GROUP BY position_id) f
              ON f.position_id = p.id
       WHERE p.status = 'closed' AND p.closed_at >= date_trunc('month', now())`,
    ),
    pool.query<{ ts: Date }>("SELECT ts FROM signals ORDER BY ts DESC LIMIT 1"),
    pool.query<{ ts: Date; regime: string; confidence: string | null; reasoning: string | null }>(
      "SELECT ts, regime, confidence, reasoning FROM market_regime WHERE asset_class='stock' ORDER BY ts DESC LIMIT 1",
    ),
    pool.query(
      `SELECT slot, profile, strategy, quant_score_min, rsi_max, sigma_min,
              target_profit_pct, stop_loss_pct, min_net_margin_eur, max_hold_seconds,
              sectors_allowed, llm_strict
       FROM slot_profiles ORDER BY slot`,
    ),
    pool.query<{ date: Date; summary: string | null; wins: number; losses: number; net_pnl: string; recommendations: unknown }>(
      "SELECT date, summary, wins, losses, net_pnl, recommendations FROM daily_reports ORDER BY date DESC LIMIT 1",
    ),
    pool.query(
      `SELECT id, ts, status, proposal, rationale FROM tuning_proposals
       WHERE status='pending' ORDER BY ts DESC LIMIT 5`,
    ),
    pool.query(
      `SELECT id, ts, symbol, slot, strategy, profile, quant_score, payload,
              llm_verdict, price, qty, target_price, stop_price, currency
       FROM pending_approvals WHERE status='pending' ORDER BY ts DESC LIMIT 20`,
    ),
    pool.query<{ id: number; ts: Date; kind: string; region: string | null; summary: string | null; candidates: unknown }>(
      "SELECT id, ts, kind, region, summary, candidates FROM briefings ORDER BY ts DESC LIMIT 1",
    ),
    pool.query<{ min_open: Date | null }>(
      "SELECT MIN(opened_at) AS min_open FROM positions WHERE status='closed'",
    ),
    pool.query<{ ts: Date; regime: string; confidence: string | null; reasoning: string | null }>(
      "SELECT ts, regime, confidence, reasoning FROM market_regime WHERE asset_class='crypto' ORDER BY ts DESC LIMIT 1",
    ),
    pool.query<{ id: number; ts: Date; side: string; status: string; limit_price: string | null; fill_price: string | null; fill_qty: string | null; symbol: string | null; slot: number | null; position_status: string | null }>(
      `SELECT o.id, o.ts, o.side, o.status, o.limit_price, o.fill_price, o.fill_qty,
              p.symbol, p.slot, p.status AS position_status
         FROM orders o LEFT JOIN positions p ON p.id = o.position_id
        WHERE o.ts::date = current_date
           OR p.status IN ('opening','open','closing')
        ORDER BY o.ts DESC LIMIT 40`,
    ),
    pool.query<{ n: string; avg_spread: string | null; avg_slip: string | null; optimism: string | null }>(
      `SELECT COUNT(*) AS n,
              AVG(spread_at_submit_bps) AS avg_spread,
              AVG(slippage_bps) AS avg_slip,
              COALESCE(SUM((fill_price - shadow_fill_price) * fill_qty), 0) AS optimism
         FROM orders
        WHERE status='filled' AND fill_price IS NOT NULL
          AND (spread_at_submit_bps IS NOT NULL OR slippage_bps IS NOT NULL)`,
    ),
    pool.query<{ wins: string; losses: string; net_pnl: string; n: string }>(
      `SELECT COUNT(*) FILTER (WHERE net_pnl_eur > 0) AS wins,
              COUNT(*) FILTER (WHERE net_pnl_eur <= 0) AS losses,
              COALESCE(SUM(net_pnl_eur), 0) AS net_pnl,
              COUNT(*) AS n
         FROM trade_outcomes
        WHERE closed_at::date = CURRENT_DATE`,
    )
  ]);

  const cfg = Object.fromEntries(configR.rows.map((r) => [r.key, r.value]));
  const universeSize = Array.isArray(cfg.UNIVERSE) ? cfg.UNIVERSE.length : 0;
  const initialNetLiq = toNum(cfg.INITIAL_NET_LIQ_EUR);
  const annualTargetPct = toNum(cfg.ANNUAL_TARGET_PCT) || 12;

  const hb = hbR.rows[0];
  const heartbeatAgeSec = hb ? Math.round((Date.now() - new Date(hb.ts).getTime()) / 1000) : null;
  const account = hb?.info?.account;

  const openPositionIds = posR.rows.map((r) => r.id);
  const ticksByPos: Record<number, PriceTick[]> = {};
  const feesByPos: Record<number, number> = {};
  if (openPositionIds.length > 0) {
    const feesR = await pool.query<{ position_id: number; fees: string | null }>(
      `SELECT position_id, COALESCE(SUM(fees), 0) AS fees
         FROM orders
        WHERE position_id = ANY($1::bigint[])
        GROUP BY position_id`,
      [openPositionIds],
    );
    for (const row of feesR.rows) feesByPos[row.position_id] = Number(row.fees ?? 0);
  }
  if (openPositionIds.length > 0) {
    const ticksR = await pool.query<{ position_id: number; ts: Date; price: string }>(
      `SELECT position_id, ts, price FROM (
         SELECT position_id, ts, price,
                ROW_NUMBER() OVER (PARTITION BY position_id ORDER BY ts DESC) AS rn
           FROM position_price_ticks
           WHERE position_id = ANY($1::bigint[])
             AND ts >= NOW() - INTERVAL '14 days'
       ) t WHERE rn <= 5000 ORDER BY position_id, ts ASC`,
      [openPositionIds],
    );
    for (const row of ticksR.rows) {
      const list = ticksByPos[row.position_id] ?? [];
      list.push({ ts: new Date(row.ts).toISOString(), price: Number(row.price) });
      ticksByPos[row.position_id] = list;
    }
  }

  const positions: Position[] = posR.rows.map((r: {
    id: number; symbol: string; slot: number; status: string;
    entry_price: string; qty: string; target_price: string; stop_price: string;
    current_price: string | null; opened_at: Date;
    sector: string | null; company_name: string | null;
  }) => {
    const entry = toNum(r.entry_price);
    const current = r.current_price !== null ? toNum(r.current_price) : entry;
    const target = toNum(r.target_price);
    const stop = toNum(r.stop_price);
    const qty = toNum(r.qty);
    const unrealizedEur = (current - entry) * qty;
    const unrealizedPct = entry > 0 ? ((current - entry) / entry) * 100 : 0;
    // Fees booked on orders belonging to this position — at open time that's
    // the entry commission. Exit commission is unknown until the SELL fills,
    // so we estimate round-trip as 2× the entry fee (IBKR tiered fees are
    // symmetric in practice for a BUY/SELL on the same notional).
    const feesPaidEur = feesByPos[r.id] ?? 0;
    const estRoundTripFees = feesPaidEur * 2;
    const unrealizedNetEur = unrealizedEur - estRoundTripFees;
    const notional = entry * qty;
    const unrealizedNetPct = notional > 0 ? (unrealizedNetEur / notional) * 100 : 0;
    const heldMs = Date.now() - new Date(r.opened_at).getTime();
    const heldDays = Math.max(0, Math.floor(heldMs / 86400000));
    const progressPct = target > entry
      ? Math.max(0, Math.min(100, ((current - entry) / (target - entry)) * 100)) : 0;
    return {
      id: r.id, symbol: r.symbol, companyName: r.company_name, sector: r.sector,
      slot: r.slot, status: r.status, entry, current, target, stop, qty,
      unrealizedEur, unrealizedPct,
      feesPaidEur, unrealizedNetEur, unrealizedNetPct,
      heldDays, progressPct,
      priceHistory: ticksByPos[r.id] ?? [],
    };
  });

  const recentTrades: Trade[] = tradeR.rows.map((r: {
    symbol: string; entry_price: string; exit_price: string;
    opened_at: Date; closed_at: Date; fees: string; net_eur: string;
    is_open: boolean;
  }) => {
    const isOpen = Boolean(r.is_open);
    const openedAtDate = new Date(r.opened_at);
    const closedAtDate = isOpen ? null : new Date(r.closed_at);
    return {
      symbol: r.symbol,
      entry: toNum(r.entry_price),
      exit: toNum(r.exit_price),
      openedAt: openedAtDate.toISOString(),
      closedAt: closedAtDate ? closedAtDate.toISOString() : null,
      heldDays: Math.max(0, Math.round(
        ((closedAtDate ?? new Date()).getTime() - openedAtDate.getTime()) / 86400000)),
      fees: toNum(r.fees),
      netEur: toNum(r.net_eur),
      isOpen,
    };
  });

  const allTimePnl = toNum(allTimeR.rows[0]?.total);
  const allTimeTrades = Number(allTimeR.rows[0]?.count ?? 0);
  const allTimeWins = Number(allTimeR.rows[0]?.wins ?? 0);
  const allTimeWinRatePct = allTimeTrades > 0 ? (allTimeWins / allTimeTrades) * 100 : 0;

  const liveNetLiq = accountNum(account, 'NetLiquidation');
  const grossPositionValue = accountNum(account, 'GrossPositionValue');
  const portfolioEur = liveNetLiq || initialNetLiq;
  const portfolioChangeEur = liveNetLiq && initialNetLiq ? liveNetLiq - initialNetLiq : 0;
  const portfolioChangePct = initialNetLiq ? (portfolioChangeEur / initialNetLiq) * 100 : 0;

  const sigRow = sigR.rows[0] as
    | { id: number; ts: Date; symbol: string; decision: string;
        quant_score: string; payload: Record<string, unknown>;
        llm_verdict: Record<string, unknown> | null; reason: string | null }
    | undefined;
  const latestSignal: Signal | null = sigRow ? {
    id: sigRow.id, ts: sigRow.ts.toISOString(),
    symbol: sigRow.symbol, decision: sigRow.decision,
    quantScore: sigRow.quant_score !== null ? toNum(sigRow.quant_score) : null,
    rsi: (sigRow.payload as { rsi?: number })?.rsi ?? null,
    llmVerdict: (sigRow.llm_verdict as { verdict?: string })?.verdict ?? null,
    reasoning: sigRow.reason ?? null,
  } : null;

  const lastSignalTs = lastSigR.rows[0]?.ts;
  const lastSignalAgeSec = lastSignalTs
    ? Math.round((Date.now() - new Date(lastSignalTs).getTime()) / 1000) : null;

  const status: StatusInfo = {
    botEnabled: cfg.BOT_ENABLED === true,
    ibConnected: hb?.info?.ib_connected === true,
    mode: (cfg.TRADING_MODE as string) ?? 'paper',
    universeSize, lastSignalAgeSec, heartbeatAgeSec,
    manualApprovalMode: cfg.MANUAL_APPROVAL_MODE === true,
    newsWatcherEnabled: cfg.NEWS_WATCHER_ENABLED === true,
  };

  const stats: Stats = {
    portfolioEur, portfolioChangeEur, portfolioChangePct,
    deployedEur: grossPositionValue,
    slotsUsed: positions.length, maxSlots: slotsR.rows.length || 18,
    monthPnl: toNum(monthR.rows[0]?.total),
    monthTrades: Number(monthR.rows[0]?.count ?? 0),
    monthWins: Number(monthR.rows[0]?.wins ?? 0),
    monthLosses: Number(monthR.rows[0]?.losses ?? 0),
    allTimePnl, allTimeTrades, allTimeWinRatePct,
  };

  const regimeRow = regimeR.rows[0];
  const regime: Regime | null = regimeRow ? {
    regime: regimeRow.regime,
    confidence: regimeRow.confidence !== null ? toNum(regimeRow.confidence) : null,
    reasoning: regimeRow.reasoning,
    ageSec: Math.round((Date.now() - new Date(regimeRow.ts).getTime()) / 1000),
  } : null;

  const regimeCryptoRow = regimeCryptoR.rows[0];
  const regimeCrypto: Regime | null = regimeCryptoRow ? {
    regime: regimeCryptoRow.regime,
    confidence: regimeCryptoRow.confidence !== null ? toNum(regimeCryptoRow.confidence) : null,
    reasoning: regimeCryptoRow.reasoning,
    ageSec: Math.round((Date.now() - new Date(regimeCryptoRow.ts).getTime()) / 1000),
  } : null;

  const slotProfiles: SlotProfile[] = slotsR.rows.map((r: {
    slot: number; profile: SlotProfile['profile']; strategy: SlotProfile['strategy'];
    quant_score_min: string; rsi_max: string; sigma_min: string;
    target_profit_pct: string; stop_loss_pct: string; min_net_margin_eur: string;
    max_hold_seconds: number | null; sectors_allowed: string[] | null; llm_strict: boolean;
  }) => ({
    slot: r.slot, profile: r.profile, strategy: r.strategy,
    quantScoreMin: toNum(r.quant_score_min),
    rsiMax: toNum(r.rsi_max),
    sigmaMin: toNum(r.sigma_min),
    targetProfitPct: toNum(r.target_profit_pct),
    stopLossPct: toNum(r.stop_loss_pct),
    minNetMarginEur: toNum(r.min_net_margin_eur),
    maxHoldSeconds: r.max_hold_seconds ?? 0,
    sectorsAllowed: r.sectors_allowed,
    llmStrict: r.llm_strict,
  }));

  const repRow = reportR.rows[0];
  const liveToday = todayLiveR.rows[0];
  const liveTodayN = liveToday ? Number(liveToday.n) : 0;
  // If today has closed trades, the dashboard headline must reflect them —
  // daily_reports is only written after 21:00 UTC and can miss a day if the
  // bot was hung (as happened 2026-04-21 19:45→23:45 UTC). Fall back to the
  // stored report only when nothing has closed today yet.
  const latestReport: DailyReport | null = liveTodayN > 0
    ? {
        date: new Date().toISOString().slice(0, 10),
        summary: repRow?.summary ?? null,
        wins: Number(liveToday.wins),
        losses: Number(liveToday.losses),
        netPnl: toNum(liveToday.net_pnl),
        recommendations: (repRow?.recommendations ?? null) as Array<{ change: string; why: string }> | null,
      }
    : repRow ? {
        date: new Date(repRow.date).toISOString().slice(0, 10),
        summary: repRow.summary, wins: repRow.wins, losses: repRow.losses,
        netPnl: toNum(repRow.net_pnl),
        recommendations: repRow.recommendations as Array<{ change: string; why: string }> | null,
      } : null;

  const pendingProposals: TuningProposal[] = proposalsR.rows.map((r: {
    id: number; ts: Date; status: string;
    proposal: { proposals?: Array<{ key: string; from: number; to: number; why: string }>; overall_rationale?: string };
    rationale: string | null;
  }) => ({
    id: r.id, ts: r.ts.toISOString(), status: r.status,
    proposals: r.proposal?.proposals ?? [],
    overallRationale: r.rationale ?? r.proposal?.overall_rationale ?? null,
  }));

  const pendingApprovals: PendingApproval[] = approvalsR.rows.map((r: {
    id: number; ts: Date; symbol: string; slot: number; strategy: string; profile: string;
    quant_score: string | null; payload: Record<string, unknown> | null;
    llm_verdict: Record<string, unknown> | null;
    price: string; qty: string; target_price: string; stop_price: string;
    currency: string;
  }) => ({
    id: r.id, ts: r.ts.toISOString(), symbol: r.symbol,
    slot: r.slot, strategy: r.strategy, profile: r.profile,
    quantScore: r.quant_score !== null ? toNum(r.quant_score) : null,
    rsi: (r.payload as { rsi?: number } | null)?.rsi ?? null,
    llmVerdict: (r.llm_verdict as { verdict?: string } | null)?.verdict ?? null,
    reasoning: (r.llm_verdict as { reasoning?: string } | null)?.reasoning ?? null,
    price: toNum(r.price), qty: toNum(r.qty),
    target: toNum(r.target_price), stop: toNum(r.stop_price),
    currency: r.currency,
  }));

  const briefRow = briefingR.rows[0];
  const latestBriefing: Briefing | null = briefRow ? {
    id: briefRow.id, ts: briefRow.ts.toISOString(),
    kind: briefRow.kind, region: briefRow.region,
    summary: briefRow.summary,
    candidates: briefRow.candidates as Array<{ symbol: string; why: string }> | null,
  } : null;

  const minOpen = openedAtStartR.rows[0]?.min_open;
  const daysActive = minOpen
    ? Math.max(1, (Date.now() - new Date(minOpen).getTime()) / 86400000) : 1;
  const totalPnl = allTimePnl + positions.reduce((s, p) => s + p.unrealizedEur, 0);
  const deployedBase = grossPositionValue || 9000; // default assumed notional when no trades
  const annualisedPct = allTimeTrades > 0
    ? ((totalPnl / deployedBase) * (365 / daysActive)) * 100 : null;

  const targetProgress: TargetProgress = {
    annualTargetPct, realizedPnl: allTimePnl,
    unrealizedPnl: positions.reduce((s, p) => s + p.unrealizedEur, 0),
    totalPnl, deployedEur: grossPositionValue, annualisedPct,
  };

  const fqRow = fillQualityR.rows[0] ?? { n: "0", avg_spread: null, avg_slip: null, optimism: "0" };
  const fillQuality: FillQuality = {
    sampleCount: Number(fqRow.n ?? 0),
    avgSpreadBps: fqRow.avg_spread != null ? Number(fqRow.avg_spread) : null,
    avgSlippageBps: fqRow.avg_slip != null ? Number(fqRow.avg_slip) : null,
    paperOptimismEur: Number(fqRow.optimism ?? 0),
  };

  const todayOrders: TodayOrder[] = todayOrdersR.rows.map((r) => ({
    id: Number(r.id),
    ts: r.ts.toISOString(),
    symbol: r.symbol,
    side: (r.side?.toUpperCase() === 'SELL' ? 'SELL' : 'BUY') as 'BUY' | 'SELL',
    status: r.status,
    limitPrice: r.limit_price != null ? toNum(r.limit_price) : null,
    fillPrice: r.fill_price != null ? toNum(r.fill_price) : null,
    fillQty: r.fill_qty != null ? toNum(r.fill_qty) : null,
    slot: r.slot,
    positionLive: (['open','opening','closing'].includes(r.position_status ?? '')),
  }));


  return {
    status, stats, regime, regimeCrypto, slotProfiles,
    positions, latestSignal, recentTrades,
    latestReport, pendingProposals, pendingApprovals,
    latestBriefing, targetProgress, todayOrders, fillQuality,
  };
}
