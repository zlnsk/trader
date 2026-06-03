import type { Trade } from '@/lib/state';

function eur(n: number, opts: { sign?: boolean } = {}): string {
  const s = Math.abs(n).toLocaleString('en-IE', {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
  const sign = opts.sign ? (n > 0 ? '+' : n < 0 ? '-' : '') : n < 0 ? '-' : '';
  return `${sign}€${s}`;
}

function fmtTs(iso: string | null): string {
  if (!iso) return '—';
  const d = new Date(iso);
  if (!Number.isFinite(d.getTime())) return '—';
  const now = new Date();
  const sameDay = d.getUTCFullYear() === now.getUTCFullYear() && d.getUTCMonth() === now.getUTCMonth() && d.getUTCDate() === now.getUTCDate();
  const hhmm = `${String(d.getUTCHours()).padStart(2,'0')}:${String(d.getUTCMinutes()).padStart(2,'0')}`;
  if (sameDay) return hhmm;
  const months = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
  const day = `${String(d.getUTCDate()).padStart(2,'0')} ${months[d.getUTCMonth()]}`;
  return `${day} ${hhmm}`;
}

export function RecentTrades({ trades }: { trades: Trade[] }) {
  if (trades.length === 0) {
    return (
      <div className="trades-card">
        <div className="empty">No trades today or yesterday.</div>
      </div>
    );
  }
  return (
    <div className="trades-card">
      <table className="trades-table">
        <thead>
          <tr>
            <th>Symbol</th>
            <th>Status</th>
            <th>Entry → Current/Exit</th>
            <th>Opened</th>
            <th>Closed</th>
            <th>Held</th>
            <th>Fees</th>
            <th>Net</th>
          </tr>
        </thead>
        <tbody>
          {trades.map((t, i) => (
            <tr key={i} className={t.isOpen ? 'row-open' : 'row-closed'}>
              <td className="sym">{t.symbol}</td>
              <td>
                <span className={`trade-status ${t.isOpen ? 'open' : 'closed'}`}>
                  {t.isOpen ? 'open' : 'closed'}
                </span>
              </td>
              <td>
                {eur(t.entry)} → {eur(t.exit)}
              </td>
              <td className="ts">{fmtTs(t.openedAt)}</td>
              <td className="ts">{t.isOpen ? '—' : fmtTs(t.closedAt)}</td>
              <td>{t.heldDays}d</td>
              <td>{eur(t.fees)}</td>
              <td className={t.netEur > 0 ? 'pos' : t.netEur < 0 ? 'neg' : ''}>
                {eur(t.netEur, { sign: true })}
                {t.isOpen && <span className="unrealized-note"> unrlzd</span>}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
