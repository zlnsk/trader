import type { DailyReport } from '@/lib/state';

function eur(n: number, opts: { sign?: boolean } = {}): string {
  const s = Math.abs(n).toLocaleString('en-IE', {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
  const sign = opts.sign ? (n > 0 ? '+' : n < 0 ? '-' : '') : n < 0 ? '-' : '';
  return `${sign}€${s}`;
}

export function DailyReportCard({ report }: { report: DailyReport | null }) {
  if (!report) {
    return (
      <div className="signal-card">
        <div className="empty">No daily report yet — Claude writes one after US market close.</div>
      </div>
    );
  }
  return (
    <div className="signal-card">
      <div className="signal-head">
        <div>
          <div className="signal-symbol" style={{ fontSize: 16 }}>{report.date}</div>
        </div>
        <div className="signal-age">
          {report.wins}W / {report.losses}L · {eur(report.netPnl, { sign: true })}
        </div>
      </div>
      {report.summary && (
        <div className="reasoning">
          <div className="reasoning-label">Summary</div>
          {report.summary}
        </div>
      )}
      {report.recommendations && report.recommendations.length > 0 && (
        <div style={{ marginTop: 12 }}>
          <div className="reasoning-label" style={{ marginBottom: 6 }}>Recommendations</div>
          <ul style={{ margin: 0, paddingLeft: 18, fontSize: 13, color: '#2e2e2e', lineHeight: 1.55 }}>
            {report.recommendations.map((r, i) => (
              <li key={i}><strong>{r.change}</strong> — {r.why}</li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}
