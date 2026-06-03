'use client';
import { useState, useTransition } from 'react';
import type { TuningProposal } from '@/lib/state';

export function TuningProposals({ proposals }: { proposals: TuningProposal[] }) {
  if (proposals.length === 0) {
    return (
      <div className="signal-card">
        <div className="empty">
          No pending tuning proposals. Claude reviews the last 7 days each Sunday and
          may suggest threshold changes; you approve or reject.
        </div>
      </div>
    );
  }
  return (
    <>
      {proposals.map((p) => (
        <Proposal key={p.id} proposal={p} />
      ))}
    </>
  );
}

function Proposal({ proposal: p }: { proposal: TuningProposal }) {
  const [pending, startTransition] = useTransition();
  const [err, setErr] = useState<string | null>(null);
  const [done, setDone] = useState<'approved' | 'rejected' | null>(null);

  const call = (action: 'approve' | 'reject') => {
    setErr(null);
    startTransition(async () => {
      try {
        const r = await fetch('/Trader/api/proposals', {
          method: 'POST',
          headers: { 'content-type': 'application/json' },
          body: JSON.stringify({ id: p.id, action }),
        });
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        setDone(action === 'approve' ? 'approved' : 'rejected');
        window.location.reload();
      } catch (e) {
        setErr(e instanceof Error ? e.message : 'failed');
      }
    });
  };

  return (
    <div className="signal-card" style={{ marginBottom: 12 }}>
      <div className="signal-head">
        <div className="signal-symbol" style={{ fontSize: 16 }}>
          Tuning proposal · {(()=>{ const d=new Date(p.ts); const months=['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec']; return `${String(d.getUTCDate()).padStart(2,'0')} ${months[d.getUTCMonth()]} ${d.getUTCFullYear()}`; })()}
        </div>
        <div className="status-actions">
          <button
            className="btn"
            disabled={pending || !!done}
            onClick={() => call('reject')}
          >
            Reject
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
      <div style={{ marginTop: 6 }}>
        {p.proposals.length === 0 ? (
          <div style={{ color: 'var(--muted)', fontSize: 13 }}>No changes proposed.</div>
        ) : (
          <table className="trades-table" style={{ width: '100%' }}>
            <thead>
              <tr><th>Key</th><th>From → To</th><th style={{ textAlign: 'left' }}>Why</th></tr>
            </thead>
            <tbody>
              {p.proposals.map((c, i) => (
                <tr key={i}>
                  <td className="sym">{c.key}</td>
                  <td>{c.from} → {c.to}</td>
                  <td style={{ textAlign: 'left' }}>{c.why}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
      {p.overallRationale && (
        <div className="reasoning" style={{ marginTop: 12 }}>
          {p.overallRationale}
        </div>
      )}
      {err && <div style={{ color: 'var(--red)', fontSize: 12, marginTop: 8 }}>{err}</div>}
    </div>
  );
}
