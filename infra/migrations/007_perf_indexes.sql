-- 007: supporting indexes for dashboard + weekly-tuning queries.
--
-- positions.status filter on ('closed' AND closed_at >= …) is scanned heavily by
-- the dashboard month/all-time aggregates and by jobs.maybe_daily_report / maybe_weekly_tuning.
-- The plain status index is too low-cardinality; add a partial index on closed rows
-- sorted by closed_at to serve both "closed this month" and "4 most recent closed".
CREATE INDEX IF NOT EXISTS positions_closed_closed_at_idx
  ON positions (closed_at DESC)
  WHERE status = 'closed';

-- signals.decision is used in maybe_weekly_tuning (FILTER on decision + ILIKE reason)
-- and in dashboard latestSignal. Add a partial index for non-skip rows and a support
-- index for (decision, ts) range scans.
CREATE INDEX IF NOT EXISTS signals_decision_ts_idx
  ON signals (decision, ts DESC);

-- Audit log is queried ad-hoc by actor during incident review; keep (actor, ts DESC).
CREATE INDEX IF NOT EXISTS audit_log_actor_ts_idx
  ON audit_log (actor, ts DESC);
