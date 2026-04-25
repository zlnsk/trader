import type { Signal } from '@/lib/state';

function ageText(ts: string): string {
  const s = Math.max(0, Math.round((Date.now() - new Date(ts).getTime()) / 1000));
  if (s < 60) return `${s}s ago`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m} min ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  return `${Math.floor(h / 24)}d ago`;
}

function decisionClass(d: string): string {
  if (d === 'skip') return 'skipped';
  if (d === 'buy') return 'buy';
  if (d === 'sell') return 'sell';
  return 'hold';
}

function decisionLabel(d: string): string {
  if (d === 'skip') return 'Skipped';
  if (d === 'buy') return 'Bought';
  if (d === 'sell') return 'Sold';
  return 'Hold';
}

export function LatestSignal({ signal }: { signal: Signal | null }) {
  if (!signal) {
    return (
      <div className="signal-card">
        <div className="empty">
          No signals yet. The engine ticks every 5 minutes during market hours.
        </div>
      </div>
    );
  }

  return (
    <div className="signal-card">
      <div className="signal-head">
        <div style={{ display: 'flex', alignItems: 'center' }}>
          <span className="signal-symbol">{signal.symbol}</span>
          <span className={`decision-badge ${decisionClass(signal.decision)}`}>
            {decisionLabel(signal.decision)}
          </span>
        </div>
        <span className="signal-age" suppressHydrationWarning>{ageText(signal.ts)}</span>
      </div>

      <div className="signal-grid">
        <div className="signal-cell">
          <div className="k">Quant score</div>
          <div className="v">
            {signal.quantScore !== null
              ? `${signal.quantScore.toFixed(0)} / 100`
              : '—'}
          </div>
        </div>
        <div className="signal-cell">
          <div className="k">RSI</div>
          <div className="v">
            {signal.rsi !== null ? signal.rsi.toFixed(1) : '—'}
          </div>
        </div>
        <div className="signal-cell">
          <div className="k">LLM verdict</div>
          <div
            className={`v${
              signal.llmVerdict?.toLowerCase() === 'veto' ? ' veto' : ''
            }`}
          >
            {signal.llmVerdict ?? '—'}
          </div>
        </div>
      </div>

      {signal.reasoning && (
        <div className="reasoning">
          <div className="reasoning-label">Reasoning</div>
          {signal.reasoning}
        </div>
      )}
    </div>
  );
}
