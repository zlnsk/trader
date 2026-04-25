import type { SlotProfile, Position } from '@/lib/state';

type Profile = 'safe' | 'balanced' | 'aggressive';
type Strategy = 'swing' | 'intraday';
const PROFILES: Profile[] = ['safe', 'balanced', 'aggressive'];
const STRATS: Strategy[] = ['swing', 'intraday'];

const STRAT_LABEL: Record<Strategy, string> = {
  swing: 'Swing (daily bars · hold days)',
  intraday: 'Intraday (5-min bars · hold hours)',
};

const DESC: Record<Profile, string> = {
  safe: 'Tight thresholds · strict LLM allow · Healthcare/Consumer only',
  balanced: 'Default thresholds · LLM abstain tolerated',
  aggressive: 'Loose thresholds · longer hold · wider target/stop',
};

function pct(n: number): string {
  const abs = Math.abs(n).toFixed(1);
  const sign = n > 0 ? '+' : n < 0 ? '-' : '';
  return `${sign}${abs}%`;
}

function formatHold(seconds: number): string {
  if (seconds >= 86400) return `≤ ${Math.round(seconds / 86400)}d`;
  if (seconds >= 3600) return `≤ ${Math.round(seconds / 3600)}h`;
  return `≤ ${Math.round(seconds / 60)}min`;
}

export function SlotConfig({
  profiles,
  positions,
}: {
  profiles: SlotProfile[];
  positions: Position[];
}) {
  const bySlot = new Map<number, Position>();
  for (const p of positions) bySlot.set(p.slot, p);

  return (
    <>
      {STRATS.map((strategy) => {
        const inStrat = profiles.filter((p) => p.strategy === strategy);
        if (inStrat.length === 0) return null;
        return (
          <div key={strategy} style={{ marginBottom: 16 }}>
            <div className="header-row" style={{ margin: '20px 0 10px' }}>
              <h2 style={{ fontSize: 15, fontWeight: 500, color: 'var(--muted)' }}>
                {STRAT_LABEL[strategy]}
              </h2>
            </div>
            <div className="slots-grid">
              {PROFILES.map((profile) => {
                const slots = inStrat.filter((p) => p.profile === profile).sort((a, b) => a.slot - b.slot);
                if (slots.length === 0) return null;
                const cfg = slots[0];
                const used = slots.filter((s) => bySlot.has(s.slot)).length;
                return (
                  <div key={profile} className={`slot-card profile-${profile}`}>
                    <div className="slot-head">
                      <div className="slot-title-row">
                        <span className={`slot-pill active ${profile}`}>{profile}</span>
                        <span className="slot-usage">{used} / {slots.length} used</span>
                      </div>
                      <div className="slot-sub">
                        target {pct(cfg.targetProfitPct)} · stop {pct(cfg.stopLossPct)} · hold {formatHold(cfg.maxHoldSeconds)}
                      </div>
                      <div className="slot-sub">
                        score ≥ {cfg.quantScoreMin} · RSI ≤ {cfg.rsiMax} · σ ≥ {cfg.sigmaMin}
                      </div>
                    </div>
                    <div className="slot-dots">
                      {slots.map((s) => {
                        const pos = bySlot.get(s.slot);
                        return (
                          <div
                            key={s.slot}
                            className={`slot-dot ${pos ? 'filled' : 'free'}`}
                            title={pos ? `Slot ${s.slot}: ${pos.symbol}` : `Slot ${s.slot}: free`}
                          >
                            {pos ? pos.symbol : `#${s.slot}`}
                          </div>
                        );
                      })}
                    </div>
                    <div className="slot-desc">{DESC[profile]}</div>
                  </div>
                );
              })}
            </div>
          </div>
        );
      })}
    </>
  );
}
