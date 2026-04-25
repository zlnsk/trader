import type { TargetProgress } from '@/lib/state';

function eur(n: number, opts: { sign?: boolean } = {}): string {
  const s = Math.abs(n).toLocaleString('en-IE', {
    minimumFractionDigits: 2, maximumFractionDigits: 2,
  });
  const sign = opts.sign ? (n > 0 ? '+' : n < 0 ? '-' : '') : n < 0 ? '-' : '';
  return `${sign}€${s}`;
}

export function TargetProgressCard({ progress }: { progress: TargetProgress }) {
  const a = progress.annualisedPct;
  const tgt = progress.annualTargetPct;
  const pctOfTarget = a != null && tgt > 0 ? Math.max(0, Math.min(100, (a / tgt) * 100)) : null;
  const behind = a != null && a < tgt;

  const realizedCls = progress.realizedPnl > 0 ? 'pos' : progress.realizedPnl < 0 ? 'neg' : '';
  const unrealCls = progress.unrealizedPnl > 0 ? 'pos' : progress.unrealizedPnl < 0 ? 'neg' : '';
  const aCls = a == null ? '' : a > 0 ? 'pos' : 'neg';

  return (
    <div className="signal-card">
      <div className="signal-head">
        <div className="signal-symbol" style={{ fontSize: 16 }}>
          Target: {tgt.toFixed(1)}% annualised
        </div>
        <div className="signal-age">on deployed capital</div>
      </div>

      {pctOfTarget != null && (
        <div className={`progress ${behind ? 'is-behind' : ''}`} aria-label={`progress to annual target: ${pctOfTarget.toFixed(0)}%`}>
          <span style={{ width: `${pctOfTarget}%` }} />
        </div>
      )}

      <div className="signal-grid" style={{ marginTop: 14 }}>
        <div className="signal-cell">
          <div className="k">Realised P&amp;L</div>
          <div className={`v ${realizedCls}`}>{eur(progress.realizedPnl, { sign: true })}</div>
        </div>
        <div className="signal-cell">
          <div className="k">Unrealised P&amp;L</div>
          <div className={`v ${unrealCls}`}>{eur(progress.unrealizedPnl, { sign: true })}</div>
        </div>
        <div className="signal-cell">
          <div className="k">Annualised so far</div>
          <div className={`v ${aCls}`}>
            {a !== null
              ? `${a > 0 ? '+' : ''}${a.toFixed(1)}%`
              : '— (need closed trades)'}
          </div>
        </div>
        {pctOfTarget != null && (
          <div className="signal-cell">
            <div className="k">% of target</div>
            <div className={`v ${behind ? 'neg' : 'pos'}`}>{pctOfTarget.toFixed(0)}%</div>
          </div>
        )}
      </div>
      <div className="reasoning" style={{ fontSize: 13 }}>
        Realistic expectation for this strategy is <strong>10–15% annualised</strong> on the
        capital the bot actually deploys (not the total account). Fees eat much of the upside
        at small notional sizes — see `MIN_NET_MARGIN_EUR` rule.
      </div>
    </div>
  );
}
