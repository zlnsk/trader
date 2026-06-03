import type { Regime } from '@/lib/state';

const LABELS: Record<string, string> = {
  mean_reversion: 'Mean-reversion',
  momentum: 'Momentum',
  risk_off: 'Risk-off',
  mixed: 'Mixed',
};

const CLS: Record<string, string> = {
  mean_reversion: 'pos',
  momentum: 'warn',
  risk_off: 'danger',
  mixed: 'muted',
};

export function RegimeBadge({
  regime,
  prefix,
}: {
  regime: Regime | null;
  prefix?: string;
}) {
  if (!regime) return null;
  const label = LABELS[regime.regime] ?? regime.regime;
  const cls = CLS[regime.regime] ?? 'muted';
  const conf = regime.confidence !== null
    ? ` · ${(regime.confidence * 100).toFixed(0)}%`
    : '';
  const lead = prefix ? `${prefix}:` : 'Regime:';
  return (
    <span
      title={regime.reasoning ?? ''}
      className={`regime-pill ${cls}`}
    >
      {lead} {label}{conf}
    </span>
  );
}
