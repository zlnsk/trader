import { NextResponse } from 'next/server';
import { pool } from '@/lib/db';

export async function GET() {
  const [
    activeRow,
    pendingCount,
    canariesRow,
    findingsRow,
    metaRow,
    enabledRow,
    flagsRows,
  ] = await Promise.all([
    pool.query(`SELECT id, rationale, source, activated_at
                FROM config_versions
                WHERE scope->>'kind'='global'
                  AND activated_at IS NOT NULL
                  AND deactivated_at IS NULL
                ORDER BY activated_at DESC LIMIT 1`),
    pool.query(`SELECT status, COUNT(*) AS n FROM tuning_proposals
                WHERE ts >= NOW() - INTERVAL '30 days'
                GROUP BY status`),
    pool.query(`SELECT id, proposal_id, slot_ids, started_at, min_trades_required
                FROM canary_assignments WHERE status='running'
                ORDER BY started_at DESC`),
    pool.query(`SELECT id, detector, severity, subject, ts
                FROM optimizer_findings
                WHERE resolved_at IS NULL
                ORDER BY ts DESC LIMIT 20`),
    pool.query(`SELECT iso_week, summary, generated_at
                FROM optimizer_meta_reports
                ORDER BY generated_at DESC LIMIT 1`),
    pool.query(`SELECT value FROM config WHERE key='OPTIMIZER_ENABLED'`),
    pool.query(`SELECT source, auto_apply, enabled FROM optimizer_source_flags`),
  ]);
  const statusCounts: Record<string, number> = {};
  for (const r of pendingCount.rows) statusCounts[r.status] = Number(r.n);
  return NextResponse.json({
    enabled: enabledRow.rows[0]?.value === true,
    active_version: activeRow.rows[0] ?? null,
    proposal_counts_30d: statusCounts,
    running_canaries: canariesRow.rows,
    recent_findings: findingsRow.rows,
    latest_meta_report: metaRow.rows[0] ?? null,
    source_flags: flagsRows.rows,
  });
}
