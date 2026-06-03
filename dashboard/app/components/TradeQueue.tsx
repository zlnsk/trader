'use client';

import { useState, useMemo } from 'react';
import type { TodayOrder, PendingApproval, Position, Signal } from '@/lib/state';
import { PriceChart } from './PriceChart';
import type { ChartRange } from '../Dashboard';

type FilterMode = 'live' | 'filled' | 'all';

function statusPill(status: string): { cls: string; label: string } {
  const s = status.toLowerCase();
  if (s === 'filled') return { cls: 'pill-win', label: 'filled' };
  if (s === 'cancelled' || s === 'canceled') return { cls: 'pill-muted', label: 'cancelled' };
  if (s === 'rejected') return { cls: 'pill-loss', label: 'rejected' };
  if (s === 'submitted' || s === 'presubmitted' || s === 'pendingsubmit') return { cls: 'pill-pending', label: 'submitted' };
  return { cls: 'pill-info', label: status };
}

function hhmm(iso: string): string {
  const d = new Date(iso);
  return `${String(d.getUTCHours()).padStart(2,'0')}:${String(d.getUTCMinutes()).padStart(2,'0')}`;
}

function fmtEur(n: number | null): string {
  if (n == null) return '—';
  return '€' + n.toLocaleString('en-IE', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

type Props = {
  orders: TodayOrder[];
  approvals: PendingApproval[];
  positions: Position[];
  signals: Signal[];
};

export function TradeQueue({ orders, approvals, positions, signals }: Props) {
  const range: ChartRange = 'Today';
  const [expandedKey, setExpandedKey] = useState<string | null>(null);
  const [filterMode, setFilterMode] = useState<FilterMode>('live');

  const filtered = useMemo(() => {
    if (filterMode === 'all') return orders;
    if (filterMode === 'live') return orders.filter((o) => o.positionLive);
    return orders.filter((o) => o.status.toLowerCase() === 'filled');
  }, [orders, filterMode]);

  const totalFills = useMemo(() => orders.filter((o) => o.status.toLowerCase() === 'filled').length, [orders]);
  const hasApprovals = approvals.length > 0;
  const hasOrders = filtered.length > 0;

  const toggle = (key: string) => setExpandedKey(expandedKey === key ? null : key);

  return (
    <div className="queue-card">
      <div className="queue-head">
        <span className="t">Trade queue</span>
        <div className="queue-head-right">
          <span className="s">
            {orders.filter((o)=>o.positionLive).length} live · {totalFills} filled · {orders.length} total · {approvals.length} awaiting
          </span>
          <div className="queue-filter" role="tablist" aria-label="Status filter">
            {(['live', 'filled', 'all'] as const).map((m) => (
              <button
                key={m}
                className={filterMode === m ? 'is-active' : ''}
                onClick={() => setFilterMode(m)}
                type="button"
              >{m}</button>
            ))}
          </div>
        </div>
      </div>

      {hasApprovals && (
        <>
          <div className="queue-section-head">Awaiting approval</div>
          {approvals.slice(0, 8).map((a) => {
            const key = `a${a.id}`;
            const isOpen = expandedKey === key;
            return (
              <div key={key}>
                <div
                  className={`queue-row ${isOpen ? 'is-expanded' : ''}`}
                  role="button"
                  tabIndex={0}
                  onClick={() => toggle(key)}
                  onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggle(key); } }}
                >
                  <span className="t-time">{hhmm(a.ts)}</span>
                  <span className="t-side buy">BUY</span>
                  <span className="t-sym">
                    {a.symbol}
                    <span className="sub">slot {a.slot}</span>
                  </span>
                  <span className="t-price">{fmtEur(a.price)}</span>
                  <span className="t-qty">×{a.qty}</span>
                  <span className="pill pill-live">click to confirm</span>
                </div>
                {isOpen && (
                  <div className="queue-detail">
                    <Detail symbol={a.symbol} positions={positions} signals={signals} range={range} />
                  </div>
                )}
              </div>
            );
          })}
        </>
      )}

      {hasOrders && (
        <div className="queue-section-head">
          {filterMode === 'live' ? 'Live positions' : filterMode === 'filled' ? 'Filled orders' : "Today's order flow"}
        </div>
      )}
      {hasOrders ? filtered.slice(0, 30).map((o) => {
        const p = statusPill(o.status);
        const price = o.fillPrice ?? o.limitPrice;
        const qty = o.fillQty ?? null;
        const key = `o${o.id}`;
        const isOpen = expandedKey === key;
        return (
          <div key={key}>
            <div
              className={`queue-row ${isOpen ? 'is-expanded' : ''}`}
              role="button"
              tabIndex={0}
              onClick={() => toggle(key)}
              onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggle(key); } }}
            >
              <span className="t-time">{hhmm(o.ts)}</span>
              <span className={`t-side ${o.side === 'BUY' ? 'buy' : 'sell'}`}>{o.side}</span>
              <span className="t-sym">
                {o.symbol ?? '—'}
                {o.slot != null && <span className="sub">slot {o.slot}</span>}
              </span>
              <span className="t-price">{fmtEur(price)}</span>
              <span className="t-qty">{qty != null ? `×${qty}` : ''}</span>
              <span className={`pill ${p.cls}`}>{p.label}</span>
            </div>
            {isOpen && (
              <div className="queue-detail">
                <div className="queue-detail-grid">
                  <DetailField label="Order id" value={String(o.id)} />
                  <DetailField label="Timestamp" value={`${String(new Date(o.ts).getUTCDate()).padStart(2,'0')} ${['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'][new Date(o.ts).getUTCMonth()]} ${String(new Date(o.ts).getUTCHours()).padStart(2,'0')}:${String(new Date(o.ts).getUTCMinutes()).padStart(2,'0')}`} />
                  <DetailField label="Side" value={o.side} />
                  <DetailField label="Status" value={o.status} />
                  <DetailField label="Limit" value={fmtEur(o.limitPrice)} />
                  <DetailField label="Fill" value={fmtEur(o.fillPrice)} />
                  <DetailField label="Fill qty" value={o.fillQty != null ? String(o.fillQty) : '—'} />
                  <DetailField label="Slot" value={o.slot != null ? String(o.slot) : '—'} />
                </div>
                {o.symbol && <Detail symbol={o.symbol} positions={positions} signals={signals} range={range} />}
              </div>
            )}
          </div>
        );
      }) : (!hasApprovals && (
        <div className="queue-empty">
          {filterMode === 'live'
            ? `No live positions. ${totalFills} filled trades closed today — toggle "filled" or "all" to see them.`
            : filterMode === 'filled'
            ? `No fills yet today. ${orders.length} other orders in flight — toggle "all" to see them.`
            : 'No orders today. Queue populates when the bot places an order.'}
        </div>
      ))}
    </div>
  );
}

function DetailField({ label, value }: { label: string; value: string }) {
  return (
    <div className="queue-detail-field">
      <div className="k">{label}</div>
      <div className="v">{value}</div>
    </div>
  );
}

function Detail({
  symbol, positions, signals, range,
}: { symbol: string; positions: Position[]; signals: Signal[]; range: ChartRange }) {
  const pos = positions.find((p) => p.symbol === symbol);
  const sig = signals.find((s) => s.symbol === symbol);
  return (
    <>
      {pos && (
        <div style={{ marginTop: 14 }}>
          <div className="queue-detail-label">Open position · {pos.companyName ?? pos.symbol}</div>
          <PriceChart
            history={pos.priceHistory}
            entry={pos.entry}
            target={pos.target}
            stop={pos.stop}
            current={pos.current}
            range={range}
          />
          <div className="queue-detail-grid" style={{ marginTop: 10 }}>
            <DetailField label="Entry" value={fmtEur(pos.entry)} />
            <DetailField label="Current" value={fmtEur(pos.current)} />
            <DetailField label="Target" value={fmtEur(pos.target)} />
            <DetailField label="Stop" value={fmtEur(pos.stop)} />
            <DetailField label="Unrealized" value={(pos.unrealizedEur >= 0 ? '+' : '-') + '€' + Math.abs(pos.unrealizedEur).toFixed(2)} />
            <DetailField label="Held" value={`${pos.heldDays}d`} />
          </div>
        </div>
      )}
      {sig && (
        <div style={{ marginTop: 14 }}>
          <div className="queue-detail-label">Latest signal · {sig.symbol}</div>
          <div className="queue-detail-grid">
            <DetailField label="Score" value={sig.quantScore != null ? sig.quantScore.toFixed(1) : '—'} />
            <DetailField label="RSI" value={sig.rsi != null ? sig.rsi.toFixed(1) : '—'} />
            <DetailField label="Decision" value={sig.decision} />
            <DetailField label="LLM verdict" value={sig.llmVerdict ?? '—'} />
          </div>
          {sig.reasoning && <div className="queue-detail-reasoning">{sig.reasoning}</div>}
        </div>
      )}
      {!pos && !sig && (
        <div className="queue-detail-muted">No open position or recent signal for {symbol}.</div>
      )}
    </>
  );
}
