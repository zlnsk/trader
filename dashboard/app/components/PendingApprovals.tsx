'use client';
import { useState, useTransition } from 'react';
import type { PendingApproval } from '@/lib/state';

function currencySymbol(cur: string): string {
  return cur === 'USD' ? '$' : cur === 'GBP' ? '£' : cur === 'CHF' ? 'CHF ' : cur === 'DKK' ? 'DKK ' : '€';
}

function n(v: number, digits = 2): string {
  return v.toLocaleString('en-IE', {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

export function PendingApprovals({ items }: { items: PendingApproval[] }) {
  if (items.length === 0) {
    return (
      <div className="signal-card">
        <div className="empty">
          Nothing awaiting approval. With <strong>Manual approval</strong> on, the bot
          queues qualifying candidates here for you to click.
        </div>
      </div>
    );
  }
  return (
    <>
      {items.map((a) => <ApprovalCard key={a.id} a={a} />)}
    </>
  );
}

function ApprovalCard({ a }: { a: PendingApproval }) {
  const [pending, startTransition] = useTransition();
  const [err, setErr] = useState<string | null>(null);
  const [done, setDone] = useState<'approved' | 'skipped' | null>(null);

  const call = (action: 'approve' | 'skip') => {
    setErr(null);
    startTransition(async () => {
      try {
        const r = await fetch('/Trader/api/approvals', {
          method: 'POST',
          headers: { 'content-type': 'application/json' },
          body: JSON.stringify({ id: a.id, action }),
        });
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        setDone(action === 'approve' ? 'approved' : 'skipped');
        window.location.reload();
      } catch (e) {
        setErr(e instanceof Error ? e.message : 'failed');
      }
    });
  };

  const cs = currencySymbol(a.currency);
  const notional = a.qty * a.price;

  return (
    <div className="signal-card" style={{ marginBottom: 12 }}>
      <div className="signal-head">
        <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
          <span className="signal-symbol">{a.symbol}</span>
          <span className={`slot-pill active ${a.profile}`}>{a.profile}</span>
          <span className="sector-badge other">slot #{a.slot} · {a.strategy}</span>
        </div>
        <div className="status-actions">
          <button className="btn" disabled={pending || !!done} onClick={() => call('skip')}>
            Skip
          </button>
          <button
            className="btn"
            style={{ color: 'var(--green)', borderColor: '#bde1cd' }}
            disabled={pending || !!done}
            onClick={() => call('approve')}
          >
            {done === 'approved' ? 'Approved' : 'Approve'}
          </button>
        </div>
      </div>
      <div className="signal-grid">
        <div className="signal-cell">
          <div className="k">Notional</div>
          <div className="v">{cs}{n(notional)}</div>
        </div>
        <div className="signal-cell">
          <div className="k">Qty @ price</div>
          <div className="v">{a.qty} @ {cs}{n(a.price)}</div>
        </div>
        <div className="signal-cell">
          <div className="k">Score / RSI</div>
          <div className="v">
            {a.quantScore !== null ? `${a.quantScore.toFixed(0)}` : '—'}
            {' / '}
            {a.rsi !== null ? a.rsi.toFixed(1) : '—'}
          </div>
        </div>
        <div className="signal-cell">
          <div className="k">Target</div>
          <div className="v" style={{ color: 'var(--green)' }}>{cs}{n(a.target)}</div>
        </div>
        <div className="signal-cell">
          <div className="k">Stop</div>
          <div className="v" style={{ color: 'var(--red)' }}>{cs}{n(a.stop)}</div>
        </div>
        <div className="signal-cell">
          <div className="k">LLM verdict</div>
          <div className="v">{a.llmVerdict ?? 'bypassed'}</div>
        </div>
      </div>
      {a.reasoning && (
        <div className="reasoning">
          <div className="reasoning-label">LLM reasoning</div>
          {a.reasoning}
        </div>
      )}
      {err && <div style={{ color: 'var(--red)', fontSize: 12, marginTop: 8 }}>{err}</div>}
    </div>
  );
}
