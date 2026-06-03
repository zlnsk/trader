import type { FillQuality } from '@/lib/state';

function fmtBps(n: number | null): string {
  if (n == null) return '—';
  const sign = n > 0 ? '+' : '';
  return `${sign}${n.toFixed(1)} bps`;
}

function fmtEur(n: number): string {
  const sign = n > 0 ? '+' : n < 0 ? '-' : '';
  return `${sign}€${Math.abs(n).toFixed(2)}`;
}

export function FillQualityCard({ fq }: { fq: FillQuality }) {
  if (fq.sampleCount === 0) {
    return (
      <div className="signal-card">
        <div className="empty">No filled orders with quote data yet. Populates after the bot places + fills its first instrumented order.</div>
      </div>
    );
  }
  const slipCls = fq.avgSlippageBps == null ? '' : fq.avgSlippageBps > 5 ? 'neg' : 'pos';
  const optimismCls = fq.paperOptimismEur > 0 ? 'neg' : 'pos';
  return (
    <div className="signal-card">
      <div className="signal-head">
        <div className="signal-symbol" style={{ fontSize: 16 }}>Fill quality</div>
        <div className="signal-age">{fq.sampleCount} sample{fq.sampleCount === 1 ? '' : 's'}</div>
      </div>
      <div className="signal-grid" style={{ marginTop: 8 }}>
        <div className="signal-cell">
          <div className="k">Avg spread at submit</div>
          <div className="v">{fmtBps(fq.avgSpreadBps)}</div>
        </div>
        <div className="signal-cell">
          <div className="k">Avg slippage vs mid</div>
          <div className={`v ${slipCls}`}>{fmtBps(fq.avgSlippageBps)}</div>
        </div>
        <div className="signal-cell">
          <div className="k">Paper optimism</div>
          <div className={`v ${optimismCls}`}>{fmtEur(fq.paperOptimismEur)}</div>
        </div>
      </div>
      <div className="reasoning" style={{ fontSize: 13 }}>
        <strong>Paper optimism</strong> = sum of (real fill − shadow fill) × qty. Shadow fill
        bakes a half-spread adverse penalty per order to model live execution drag. Reddit
        research shows real slippage is <strong>2-3× commissions</strong> and invisible on paper —
        this surfaces the gap before going live.
      </div>
    </div>
  );
}
