'use client';
import { useEffect, useState } from 'react';

type Position = {
  id: number;
  symbol: string;
  slot: number;
  status: string;
  entry_price: string | null;
  exit_price: string | null;
  qty: string | null;
  current_price: string | null;
  opened_at: string;
  closed_at: string | null;
  sector: string | null;
  company_name: string | null;
};

type PendingOrder = {
  id: number;
  position_id: number;
  side: 'BUY' | 'SELL';
  status: string;
  client_order_id: string;
  ts: string;
  symbol: string;
  slot: number;
};

type ClosedRow = {
  id: number;
  symbol: string;
  entry_price: string;
  exit_price: string;
  qty: string;
  opened_at: string;
  closed_at: string;
  return_pct: string;
};

type Signal = {
  id: number;
  ts: string;
  symbol: string;
  quant_score: string | null;
  decision: string;
  reason: string | null;
  payload: Record<string, unknown>;
};

type OvernightState = {
  strategy: 'overnight';
  enabled: boolean;
  open_positions: Position[];
  pending_orders: PendingOrder[];
  metrics: {
    closed_count: number;
    wins: number;
    losses: number;
    win_rate: number | null;
    cumulative_pnl_eur: number;
    avg_return_pct: number;
  };
  recent_closed: ClosedRow[];
  recent_signals: Signal[];
};

function fmtEur(n: number): string {
  const sign = n >= 0 ? '+' : '-';
  return `${sign}${Math.abs(n).toFixed(2)}€`;
}

function fmtPct(n: number | null): string {
  if (n === null || Number.isNaN(n)) return '—';
  const sign = n >= 0 ? '+' : '';
  return `${sign}${n.toFixed(2)}%`;
}

function fmtTime(iso: string): string {
  const d = new Date(iso);
  const months = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
  return `${String(d.getUTCDate()).padStart(2,'0')} ${months[d.getUTCMonth()]} ${String(d.getUTCHours()).padStart(2,'0')}:${String(d.getUTCMinutes()).padStart(2,'0')}`;
}

export function OvernightView({ initial }: { initial: OvernightState }) {
  const [state, setState] = useState<OvernightState>(initial);

  useEffect(() => {
    let alive = true;
    const tick = async () => {
      if (typeof document !== 'undefined' && document.hidden) return;
      try {
        const r = await fetch('/Trader/api/strategies/overnight', { cache: 'no-store' });
        if (!r.ok) return;
        const next = (await r.json()) as OvernightState;
        if (alive) setState(next);
      } catch { /* ignore */ }
    };
    const id = setInterval(tick, 10000);
    const onVis = () => { if (!document.hidden) tick(); };
    document.addEventListener('visibilitychange', onVis);
    return () => {
      alive = false;
      clearInterval(id);
      document.removeEventListener('visibilitychange', onVis);
    };
  }, []);

  const m = state.metrics;
  const pnlStr = fmtEur(m.cumulative_pnl_eur);
  const wrStr = m.win_rate === null ? '—' : `${(m.win_rate * 100).toFixed(1)}%`;

  return (
    <>
      <nav className="nav" aria-label="Primary">
        <div className="nav-left">
          <a className="brand" href="/Trader/">
            <span className="brand-mark" aria-hidden />
            <span className="brand-name">Trader</span>
          </a>
          <div className="nav-right" style={{ marginLeft: 32 }}>
            <a className="nav-link" href="/Trader/">Mean-Rev</a>
            <a className="nav-link is-active" href="/Trader/strategies/overnight">Overnight</a>
          </div>
        </div>
        <div className="nav-right">
          <span
            className="badge"
            data-tone={state.enabled ? 'ok' : 'muted'}
            title={state.enabled ? 'OVERNIGHT_ENABLED=true' : 'OVERNIGHT_ENABLED=false'}
          >
            {state.enabled ? 'Enabled' : 'Disabled'}
          </span>
        </div>
      </nav>

      <main className="hero">
        <div className="hero-head">
          <span className="hero-eyebrow">Overnight Edge</span>
          <h1 className="hero-title">MOC → MOO, US large-caps</h1>
          <p className="hero-sub">
            Buy the close, sell the next open. 5 slots (25–29). SPY-trend gated,
            earnings-clear, momentum-ranked. Paper-only.
          </p>
        </div>

        <div className="stat-row">
          <div className="stat">
            <div className="stat-label">Cumulative P/L</div>
            <div className="stat-value" data-tone={m.cumulative_pnl_eur >= 0 ? 'ok' : 'warn'}>
              {pnlStr}
            </div>
          </div>
          <div className="stat">
            <div className="stat-label">Win rate</div>
            <div className="stat-value">{wrStr}</div>
            <div className="stat-sub">
              {m.wins}W / {m.losses}L · {m.closed_count} closed
            </div>
          </div>
          <div className="stat">
            <div className="stat-label">Avg return</div>
            <div className="stat-value">{fmtPct(m.avg_return_pct)}</div>
          </div>
          <div className="stat">
            <div className="stat-label">Open now</div>
            <div className="stat-value">{state.open_positions.length}</div>
            <div className="stat-sub">{state.pending_orders.length} pending orders</div>
          </div>
        </div>
      </main>

      <section className="section">
        <h2 className="section-head">Open positions</h2>
        {state.open_positions.length === 0 ? (
          <p className="muted">No overnight positions held.</p>
        ) : (
          <table className="table">
            <thead>
              <tr>
                <th>Symbol</th><th>Slot</th><th>Status</th><th>Qty</th>
                <th>Entry</th><th>Current</th><th>Opened</th>
              </tr>
            </thead>
            <tbody>
              {state.open_positions.map((p) => (
                <tr key={p.id}>
                  <td>{p.symbol}</td>
                  <td>{p.slot}</td>
                  <td>{p.status}</td>
                  <td>{p.qty ?? '—'}</td>
                  <td>{p.entry_price ?? '—'}</td>
                  <td>{p.current_price ?? '—'}</td>
                  <td>{fmtTime(p.opened_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>

      <section className="section">
        <h2 className="section-head">Pending MOC / MOO orders</h2>
        {state.pending_orders.length === 0 ? (
          <p className="muted">No pending orders.</p>
        ) : (
          <table className="table">
            <thead>
              <tr><th>Symbol</th><th>Slot</th><th>Side</th><th>Status</th><th>Submitted</th></tr>
            </thead>
            <tbody>
              {state.pending_orders.map((o) => (
                <tr key={o.id}>
                  <td>{o.symbol}</td>
                  <td>{o.slot}</td>
                  <td>{o.side} {o.side === 'BUY' ? 'MOC' : 'MOO'}</td>
                  <td>{o.status}</td>
                  <td>{fmtTime(o.ts)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>

      <section className="section">
        <h2 className="section-head">Recent closed</h2>
        {state.recent_closed.length === 0 ? (
          <p className="muted">No closed overnight trades yet.</p>
        ) : (
          <table className="table">
            <thead>
              <tr>
                <th>Symbol</th><th>Qty</th><th>Entry</th><th>Exit</th>
                <th>Return</th><th>Opened</th><th>Closed</th>
              </tr>
            </thead>
            <tbody>
              {state.recent_closed.map((r) => {
                const ret = Number(r.return_pct);
                return (
                  <tr key={r.id}>
                    <td>{r.symbol}</td>
                    <td>{r.qty}</td>
                    <td>{r.entry_price}</td>
                    <td>{r.exit_price}</td>
                    <td data-tone={ret >= 0 ? 'ok' : 'warn'}>{fmtPct(ret)}</td>
                    <td>{fmtTime(r.opened_at)}</td>
                    <td>{fmtTime(r.closed_at)}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </section>

      <section className="section">
        <h2 className="section-head">Recent signals</h2>
        {state.recent_signals.length === 0 ? (
          <p className="muted">No signals logged yet.</p>
        ) : (
          <table className="table">
            <thead>
              <tr>
                <th>Time</th><th>Symbol</th><th>Decision</th>
                <th>Score</th><th>Reason</th>
              </tr>
            </thead>
            <tbody>
              {state.recent_signals.map((s) => (
                <tr key={s.id}>
                  <td>{fmtTime(s.ts)}</td>
                  <td>{s.symbol}</td>
                  <td>{s.decision}</td>
                  <td>{s.quant_score ?? '—'}</td>
                  <td className="muted">{s.reason ?? '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
    </>
  );
}
