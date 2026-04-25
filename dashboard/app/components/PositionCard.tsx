'use client';

import { useState } from 'react';
import type { Position } from '@/lib/state';
import { PriceChart } from './PriceChart';
import type { ChartRange } from '../Dashboard';

function eur(n: number, opts: { sign?: boolean } = {}): string {
  const s = Math.abs(n).toLocaleString('en-IE', {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
  const sign = opts.sign ? (n > 0 ? '+' : n < 0 ? '-' : '') : n < 0 ? '-' : '';
  return `${sign}€${s}`;
}

function pct(n: number): string {
  const abs = Math.abs(n).toFixed(2);
  const sign = n > 0 ? '+' : n < 0 ? '-' : '';
  return `${sign}${abs}%`;
}

function sectorClass(sector: string | null): string {
  const s = (sector ?? 'other').toLowerCase();
  if (s.includes('health')) return 'healthcare';
  if (s.includes('tech')) return 'tech';
  if (s.includes('fin')) return 'finance';
  if (s.includes('consumer')) return 'consumer';
  if (s.includes('energy')) return 'energy';
  return 'other';
}

export function PositionCard({ position: p }: { position: Position }) {
  const [range, setRange] = useState<ChartRange>('Today');
  // Headline colour keys off NET unrealized — so a tiny gross gain that goes
  // underwater once fees are accounted for shows as a loss, not a win.
  const changeClass = p.unrealizedNetEur > 0 ? 'pos' : p.unrealizedNetEur < 0 ? 'neg' : '';
  const targetPct = p.entry > 0 ? ((p.target - p.entry) / p.entry) * 100 : 0;
  const stopPct = p.entry > 0 ? ((p.stop - p.entry) / p.entry) * 100 : 0;
  const roundTripFees = p.feesPaidEur * 2;

  const foot =
    p.current >= p.entry
      ? `Held ${p.heldDays}d · ${p.progressPct.toFixed(0)}% to target`
      : `Held ${p.heldDays}d · below entry`;

  return (
    <div className="position-card">
      <div className="pos-head">
        <div className="pos-head-left">
          <span className="pos-symbol">{p.symbol}</span>
          {p.companyName && <span className="pos-name">{p.companyName}</span>}
          {p.sector && (
            <span className={`sector-badge ${sectorClass(p.sector)}`}>
              {p.sector}
            </span>
          )}
        </div>
        <div className={`pos-change ${changeClass}`}>
          <div>
            {eur(p.unrealizedNetEur, { sign: true })} ({pct(p.unrealizedNetPct)})
            <span className="pos-change-label"> net</span>
          </div>
          <div className="pos-change-sub">
            gross {eur(p.unrealizedEur, { sign: true })} · fees {eur(roundTripFees)} rt
          </div>
        </div>
      </div>

      <div className="pos-grid">
        <div className="pos-cell">
          <div className="k">Entry</div>
          <div className="v">{eur(p.entry)}</div>
        </div>
        <div className="pos-cell">
          <div className="k">Current</div>
          <div className="v">{eur(p.current)}</div>
        </div>
        <div className="pos-cell">
          <div className="k">Target</div>
          <div className="v">
            {eur(p.target)} ({pct(targetPct)})
          </div>
        </div>
        <div className="pos-cell">
          <div className="k">Stop</div>
          <div className="v">
            {eur(p.stop)} ({pct(stopPct)})
          </div>
        </div>
      </div>

      <div className="chart-range">
        {(['Today', '14d'] as const).map((r) => (
          <button
            key={r}
            type="button"
            className={range === r ? 'is-active' : ''}
            onClick={() => setRange(r)}
          >{r}</button>
        ))}
      </div>
      <PriceChart history={p.priceHistory} entry={p.entry} target={p.target} stop={p.stop} current={p.current} range={range} />

      <div className="pos-progress">
        <div
          className="pos-progress-fill"
          style={{ width: `${Math.max(0, Math.min(100, p.progressPct))}%` }}
        />
      </div>

      <div className="pos-foot">{foot}</div>
    </div>
  );
}
