import type { Briefing } from '@/lib/state';

const LABEL: Record<string, string> = {
  pre_open_eu: 'Pre-open · EU',
  pre_open_us: 'Pre-open · US',
  end_of_day: 'End-of-day',
};

function ageText(ts: string): string {
  const s = Math.max(0, Math.round((Date.now() - new Date(ts).getTime()) / 1000));
  if (s < 60) return `${s}s ago`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m} min ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  return `${Math.floor(h / 24)}d ago`;
}

export function BriefingCard({ briefing }: { briefing: Briefing | null }) {
  if (!briefing) {
    return (
      <div className="signal-card">
        <div className="empty">
          No briefing yet. Claude writes EU briefing at 06:45 UTC and US briefing at 13:15 UTC each weekday.
        </div>
      </div>
    );
  }
  return (
    <div className="signal-card">
      <div className="signal-head">
        <div className="signal-symbol" style={{ fontSize: 16 }}>
          {LABEL[briefing.kind] ?? briefing.kind}
        </div>
        <div className="signal-age" suppressHydrationWarning>{ageText(briefing.ts)}</div>
      </div>
      {briefing.summary && (
        <div className="reasoning">
          <div className="reasoning-label">Summary</div>
          {briefing.summary}
        </div>
      )}
      {briefing.candidates && briefing.candidates.length > 0 && (
        <div style={{ marginTop: 12 }}>
          <div className="reasoning-label" style={{ marginBottom: 6 }}>Top watch-list</div>
          <ul style={{ margin: 0, paddingLeft: 18, fontSize: 13, color: '#2e2e2e', lineHeight: 1.55 }}>
            {briefing.candidates.map((c, i) => (
              <li key={i}><strong>{c.symbol}</strong> — {c.why}</li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}
