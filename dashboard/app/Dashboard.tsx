'use client';
import { useEffect, useState } from 'react';
import type { State } from '@/lib/state';
import { PositionCard } from './components/PositionCard';
import { LatestSignal } from './components/LatestSignal';
import { RecentTrades } from './components/RecentTrades';
import { SlotConfig } from './components/SlotConfig';
import { TuningProposals } from './components/TuningProposals';
import { PendingApprovals } from './components/PendingApprovals';
import { TargetProgressCard } from './components/TargetProgressCard';
import { ModeToggles } from './components/ModeToggles';
import { TradeQueue } from './components/TradeQueue';
import { FillQualityCard } from './components/FillQualityCard';

export type ChartRange = 'Today' | '14d';

export function Dashboard({ initial }: { initial: State }) {
  const [state, setState] = useState<State>(initial);


  useEffect(() => {
    let alive = true;
    const tick = async () => {
      if (typeof document !== 'undefined' && document.hidden) return;
      try {
        const r = await fetch('/Trader/api/state', { cache: 'no-store' });
        if (!r.ok) return;
        const next = (await r.json()) as State;
        if (alive) setState(next);
      } catch { /* ignore */ }
    };
    const id = setInterval(tick, 5000);
    const onVis = () => { if (!document.hidden) tick(); };
    document.addEventListener('visibilitychange', onVis);
    return () => {
      alive = false;
      clearInterval(id);
      document.removeEventListener('visibilitychange', onVis);
    };
  }, []);

  useEffect(() => {
    if (typeof document === 'undefined') return;
    const stats = state.stats;
    const v = Math.round(stats.portfolioEur || 0).toLocaleString('en-US');
    const pnl = stats.portfolioChangeEur;
    const pnlSign = pnl >= 0 ? '+' : '-';
    const pnlStr = `${pnlSign}${Math.abs(pnl).toFixed(0)}€`;
    const mark = pnl >= 0 ? '▲' : '▼';
    document.title = `${mark} ${pnlStr} · ${v} € · Trader`;
  }, [state.stats.portfolioEur, state.stats.portfolioChangeEur]);

  const s = state.status;
  const st = state.stats;
  const reg = state.regime?.regime ?? 'unknown';

  const hbHealthy = s.heartbeatAgeSec != null && s.heartbeatAgeSec < 30;
  const changeUp = st.portfolioChangeEur >= 0;
  const allUp = st.allTimePnl >= 0;

  const heroBig = Math.round(st.portfolioEur || 0).toLocaleString('en-US');
  const todayPnlStr = (changeUp ? '+' : '-') + Math.abs(st.portfolioChangeEur).toFixed(2);
  const allPnlStr = (allUp ? '+' : '-') + Math.abs(st.allTimePnl).toFixed(0);

  return (
    <>
      <nav className="nav" aria-label="Primary">
        <div className="nav-left">
          <a className="brand" href="#top">
            <span className="brand-mark" aria-hidden />
            <span className="brand-name">Trader</span>
          </a>
          <div className="nav-right" style={{ marginLeft: 32 }}>
            <a className="nav-link is-active" href="#top">Today</a>
            <a className="nav-link" href="#positions">Positions</a>
            <a className="nav-link" href="#slots">Slots</a>
            <a className="nav-link" href="#signals">Signals</a>
            <a className="nav-link" href="#optimizer">Optimizer</a>
          </div>
        </div>
        <div className="sync-group">
          <span className={`sync-pill ${s.botEnabled ? '' : 'is-error'}`}>
            <span className="sync-dot" />
            <span>Bot · {s.botEnabled ? 'armed' : 'paused'}</span>
          </span>
          <span className={`sync-pill ${s.ibConnected ? '' : 'is-error'}`}>
            <span className="sync-dot" />
            <span>IBKR · {s.ibConnected ? 'connected' : 'down'}</span>
          </span>
          <span className="sync-pill">
            <span className="sync-dot" />
            <span>{s.mode}</span>
          </span>
        </div>
      </nav>

      <section className="status-bar" id="top" aria-label="Status">
        <div className="sb-left">
          <span className={`sb-portfolio ${changeUp ? 'up' : 'down'}`}>{heroBig} €</span>
          <span className={`sb-pnl ${changeUp ? 'up' : 'down'}`} title="Today P&L">{todayPnlStr} €</span>
          <span className={`sb-pct ${changeUp ? 'up' : 'down'}`}>{changeUp ? '▲' : '▼'} {Math.abs(st.portfolioChangePct).toFixed(2)}%</span>
          <span className="sb-meta">{reg} · {s.mode} · {s.universeSize} syms · {st.slotsUsed}/{st.maxSlots} slots{s.manualApprovalMode ? ' · manual' : ''}</span>
        </div>
        <div className="sb-right">
          <span className={`sb-stat ${hbHealthy ? 'ok' : 'err'}`}><em>♥</em>{s.heartbeatAgeSec == null ? '—' : String(Math.round(s.heartbeatAgeSec)) + 's'}</span>
          <span className={`sb-stat ${st.monthPnl >= 0 ? 'ok' : 'err'}`}>Mo {st.monthWins}–{st.monthLosses} · {(st.monthPnl >= 0 ? '+' : '-')}{Math.abs(st.monthPnl).toFixed(0)} €</span>
          <span className={`sb-stat ${allUp ? 'ok' : 'err'}`}>All {allPnlStr} € · {st.allTimeTrades}t · {st.allTimeWinRatePct.toFixed(0)}%</span>
        </div>
      </section>
      <TargetBar progress={state.targetProgress} />

      <div className="grid">
        <div className="card col-12">
          <TradeQueue
            orders={state.todayOrders}
            approvals={state.pendingApprovals}
            positions={state.positions}
            signals={state.latestSignal ? [state.latestSignal] : []}
          />
        </div>

        <div className="card col-12" id="positions">
          <div className="card-head">
            <div className="card-title">
              <span className="s">Open positions</span>
              <span className="t">{state.positions.length === 0 ? 'none' : `${state.positions.length} active`}</span>
            </div>
            <span className="card-badge">{s.mode}</span>
          </div>
          {state.positions.length === 0 ? (
            <div className="empty">No open positions. Bot buys when a qualifying dip passes all gates for an available slot.</div>
          ) : (
            state.positions.map(p => <PositionCard key={p.id} position={p} />)
          )}
        </div>

        <div className="card col-8" id="signals">
          <div className="card-head">
            <div className="card-title">
              <span className="s">Latest signal</span>
              <span className="t">{state.latestSignal?.symbol ?? 'idle'}</span>
            </div>
          </div>
          <LatestSignal signal={state.latestSignal} />
        </div>

        <div className="card col-4">
          <div className="card-head">
            <div className="card-title">
              <span className="s">Regime</span>
              <span className="t">{state.regime?.regime ?? 'pending'}</span>
            </div>
            <span className="card-badge">conf {Math.round((state.regime?.confidence ?? 0) * 100)}%</span>
          </div>
          <p style={{ color: 'var(--ink-2)', fontSize: 14, lineHeight: 1.6 }}>
            {state.regime?.reasoning ?? '—'}
          </p>
          {state.regimeCrypto && (
            <p style={{ color: 'var(--ink-3)', fontSize: 13, lineHeight: 1.55, marginTop: 10 }}>
              Crypto · <strong style={{ color: 'var(--ink)' }}>{state.regimeCrypto.regime}</strong>
              {state.regimeCrypto.confidence != null ? ` · conf ${Math.round(state.regimeCrypto.confidence * 100)}%` : ''}
            </p>
          )}
        </div>

        <div className="card col-12">
          <div className="card-head">
            <div className="card-title">
              <span className="s">Target progress</span>
              <span className="t">annual gate</span>
            </div>
          </div>
          <TargetProgressCard progress={state.targetProgress} />
        </div>

        <div className="card col-12" id="slots">
          <div className="card-head">
            <div className="card-title">
              <span className="s">Slots</span>
              <span className="t">{state.slotProfiles.length} configured</span>
            </div>
          </div>
          <SlotConfig profiles={state.slotProfiles} positions={state.positions} />
        </div>

        <div className="card col-12">
          <div className="card-head">
            <div className="card-title">
              <span className="s">Modes</span>
              <span className="t">live switches</span>
            </div>
          </div>
          <ModeToggles status={s} />
        </div>

        {s.manualApprovalMode && (
          <div className="card col-12">
            <div className="card-head">
              <div className="card-title">
                <span className="s">Approvals</span>
                <span className="t">{state.pendingApprovals.length === 0 ? 'none' : `${state.pendingApprovals.length} awaiting`}</span>
              </div>
            </div>
            <PendingApprovals items={state.pendingApprovals} />
          </div>
        )}

        <div className="card col-12" id="optimizer">
          <div className="card-head">
            <div className="card-title">
              <span className="s">Tuning proposals</span>
              <span className="t">{state.pendingProposals.length === 0 ? 'none pending' : `${state.pendingProposals.length} pending`}</span>
            </div>
          </div>
          <TuningProposals proposals={state.pendingProposals} />
        </div>

        <div className="card col-12">
          <div className="card-head">
            <div className="card-title">
              <span className="s">Recent trades</span>
              <span className="t">today + yesterday</span>
            </div>
          </div>
          <RecentTrades trades={state.recentTrades} />
        </div>
      </div>
    </>
  );
}



function TargetBar({ progress }: { progress: { annualTargetPct: number; realizedPnl: number; unrealizedPnl: number; totalPnl: number; deployedEur: number; annualisedPct: number | null } }) {
  const a = progress.annualisedPct;
  const tgt = progress.annualTargetPct;
  const pctOfTarget = a != null && tgt > 0 ? Math.max(0, Math.min(100, (a / tgt) * 100)) : null;
  const realizedCls = progress.realizedPnl > 0 ? "up" : progress.realizedPnl < 0 ? "down" : "";
  const unrealCls = progress.unrealizedPnl > 0 ? "up" : progress.unrealizedPnl < 0 ? "down" : "";
  const aCls = a == null ? "" : a > 0 ? "up" : "down";
  const sign = (n: number) => (n > 0 ? "+" : n < 0 ? "-" : "");
  return (
    <div className="target-bar" aria-label="Annual target progress">
      <div className="tb-left">
        <span className="tb-label">Annual target</span>
        <span className="tb-goal">{tgt.toFixed(0)}%</span>
        {pctOfTarget != null && (
          <div className="tb-progress"><span style={{ width: `${pctOfTarget}%` }} /></div>
        )}
        <span className={`tb-annualised ${aCls}`}>{a != null ? `${sign(a)}${Math.abs(a).toFixed(1)}% ann.` : "—"}</span>
      </div>
      <div className="tb-right">
        <span className={`tb-pnl ${realizedCls}`}>Realised {sign(progress.realizedPnl)}€{Math.abs(progress.realizedPnl).toFixed(2)}</span>
        <span className={`tb-pnl ${unrealCls}`}>Unrealised {sign(progress.unrealizedPnl)}€{Math.abs(progress.unrealizedPnl).toFixed(2)}</span>
        <span className="tb-deployed">Deployed €{Math.round(progress.deployedEur).toLocaleString("en-US")}</span>
      </div>
    </div>
  );
}
