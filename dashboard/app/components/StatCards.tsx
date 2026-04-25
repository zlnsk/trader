import type { Stats } from '@/lib/state';

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

export function StatCards({ stats }: { stats: Stats }) {
  const changeClass =
    stats.portfolioChangeEur > 0
      ? 'pos'
      : stats.portfolioChangeEur < 0
        ? 'neg'
        : '';
  const monthClass =
    stats.monthPnl > 0 ? 'pos' : stats.monthPnl < 0 ? 'neg' : '';
  const allTimeClass =
    stats.allTimePnl > 0 ? 'pos' : stats.allTimePnl < 0 ? 'neg' : '';

  return (
    <div className="stat-grid">
      <div className="stat-card">
        <div className="stat-label">Portfolio value</div>
        <div className="stat-value">{eur(stats.portfolioEur)}</div>
        <div className={`stat-sub ${changeClass}`}>
          {eur(stats.portfolioChangeEur, { sign: true })} (
          {pct(stats.portfolioChangePct)})
        </div>
      </div>

      <div className="stat-card">
        <div className="stat-label">Deployed</div>
        <div className="stat-value">{eur(stats.deployedEur)}</div>
        <div className="stat-sub">
          {stats.slotsUsed} of {stats.maxSlots} slots used
        </div>
      </div>

      <div className="stat-card">
        <div className="stat-label">This month</div>
        <div className={`stat-value ${monthClass}`}>
          {eur(stats.monthPnl, { sign: true })}
        </div>
        <div className="stat-sub">
          {stats.monthTrades} trade{stats.monthTrades === 1 ? '' : 's'}
          {stats.monthTrades > 0 &&
            ` · ${stats.monthWins}W / ${stats.monthLosses}L`}
        </div>
      </div>

      <div className="stat-card">
        <div className="stat-label">All time</div>
        <div className={`stat-value ${allTimeClass}`}>
          {eur(stats.allTimePnl, { sign: true })}
        </div>
        <div className="stat-sub">
          {stats.allTimeTrades === 0
            ? 'no trades yet'
            : `${stats.allTimeWinRatePct.toFixed(0)}% win rate`}
        </div>
      </div>
    </div>
  );
}
