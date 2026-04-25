'use client';
import { useState, useTransition } from 'react';
import type { StatusInfo, Regime } from '@/lib/state';
import { RegimeBadge } from './RegimeBadge';

function ageText(s: number | null): string {
  if (s === null) return 'never';
  if (s < 60) return `${s}s ago`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m} min ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  return `${Math.floor(h / 24)}d ago`;
}

export function StatusBar({
  status,
  regime,
  regimeCrypto,
}: {
  status: StatusInfo;
  regime: Regime | null;
  regimeCrypto: Regime | null;
}) {
  const [pending, startTransition] = useTransition();
  const [err, setErr] = useState<string | null>(null);

  const healthy = status.botEnabled && status.ibConnected;
  const paused = !status.botEnabled;
  const dotClass = healthy ? '' : paused ? ' paused' : ' down';

  const label = !status.ibConnected
    ? 'Broker offline'
    : status.botEnabled
      ? 'Bot active'
      : 'Bot paused';

  const meta = `Scanning ${status.universeSize} symbol${
    status.universeSize === 1 ? '' : 's'
  } · Last signal ${ageText(status.lastSignalAgeSec)}`;

  const call = (url: string, body?: unknown) => {
    startTransition(async () => {
      setErr(null);
      try {
        const r = await fetch(url, {
          method: 'POST',
          headers: { 'content-type': 'application/json' },
          body: body ? JSON.stringify(body) : undefined,
        });
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        window.location.reload();
      } catch (e) {
        setErr(e instanceof Error ? e.message : 'failed');
      }
    });
  };

  return (
    <div>
      <div className="status-bar">
        <div className={`status-dot${dotClass}`} />
        <div className="status-label">{label}</div>
        <div className="status-meta">{meta}</div>
        <RegimeBadge regime={regime} prefix="Equity" />
        <RegimeBadge regime={regimeCrypto} prefix="Crypto" />
        <div className="status-actions">
          <button
            className="btn"
            disabled={pending}
            onClick={() =>
              call('/Trader/api/pause', {
                enabled: !status.botEnabled,
                confirm: status.botEnabled ? 'PAUSE' : 'RESUME',
              })
            }
          >
            {status.botEnabled ? 'Pause' : 'Resume'}
          </button>
          <button
            className="btn danger"
            disabled={pending || !status.botEnabled}
            onClick={() => {
              if (!window.confirm('Kill switch: force-disable the bot?')) return;
              call('/Trader/api/kill-switch', { confirm: 'KILL' });
            }}
          >
            Kill switch
          </button>
        </div>
      </div>
      {err && (
        <div style={{ color: 'var(--red)', fontSize: 12, marginTop: 8 }}>
          {err}
        </div>
      )}
    </div>
  );
}
