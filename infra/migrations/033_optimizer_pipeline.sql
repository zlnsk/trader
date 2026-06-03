-- 033: optimizer pipeline tables.
--
-- Extends existing tuning_proposals with columns the new pipeline needs.
-- Adds findings, canary_assignments, apply_events, rollback_events, and
-- meta_reports. All optimizer-written; trader reads only what it must
-- (active canary assignments for per-slot config override).
--
-- Pipeline flow:
--   finding  --(generates)-->  tuning_proposals
--   tuning_proposals  --(adversary validates)-->  status=validated|rejected
--   tuning_proposals  --(canary deploys)-->  canary_assignments
--   canary_assignments  --(pass)-->  config_versions (apply)  +  apply_events
--   apply_events  --(rollback triggered)-->  rollback_events

BEGIN;

-- Extend existing tuning_proposals.
ALTER TABLE tuning_proposals
  ADD COLUMN IF NOT EXISTS source           TEXT,
  ADD COLUMN IF NOT EXISTS finding_id       BIGINT,
  ADD COLUMN IF NOT EXISTS adversary_result JSONB,
  ADD COLUMN IF NOT EXISTS adversary_ts     TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS canary_id        BIGINT,
  ADD COLUMN IF NOT EXISTS applied_version_id INTEGER REFERENCES config_versions(id),
  ADD COLUMN IF NOT EXISTS rolled_back_at   TIMESTAMPTZ;

-- Relax the status check to include new pipeline states. Old rows ('pending',
-- 'approved','rejected','applied') remain valid.
ALTER TABLE tuning_proposals DROP CONSTRAINT IF EXISTS tuning_proposals_status_check;
ALTER TABLE tuning_proposals ADD CONSTRAINT tuning_proposals_status_check
  CHECK (status = ANY (ARRAY[
    'pending','validated','rejected','approved','applied',
    'canary_running','canary_passed','canary_failed','rolled_back',
    'awaiting_human','superseded'
  ]));

-- Close the FK loop from config_versions -> tuning_proposals.
ALTER TABLE config_versions
  DROP CONSTRAINT IF EXISTS fk_config_versions_proposal,
  ADD CONSTRAINT fk_config_versions_proposal
  FOREIGN KEY (proposal_id) REFERENCES tuning_proposals(id) DEFERRABLE INITIALLY DEFERRED;

-- Findings: anomaly detector + opportunity detector write here.
CREATE TABLE IF NOT EXISTS optimizer_findings (
  id                  BIGSERIAL PRIMARY KEY,
  ts                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  detector            TEXT NOT NULL,        -- 'drawdown_breach', 'pf_regression', 'loss_cluster', 'win_cluster', 'frequency_collapse', 'data_quality', 'llm_strategic'
  severity            TEXT NOT NULL CHECK (severity IN ('info','warning','critical')),
  subject             TEXT NOT NULL,        -- human-readable title
  body                TEXT,
  evidence            JSONB,                -- e.g. {"slot":14, "window_days":7, "pf_now":0.42, "pf_baseline":1.2, "n_samples":63}
  config_version_id   INTEGER REFERENCES config_versions(id),
  proposal_id         BIGINT REFERENCES tuning_proposals(id),
  resolved_at         TIMESTAMPTZ,
  resolution          TEXT
);
CREATE INDEX IF NOT EXISTS idx_optimizer_findings_ts       ON optimizer_findings (ts DESC);
CREATE INDEX IF NOT EXISTS idx_optimizer_findings_detector ON optimizer_findings (detector, ts DESC);

-- Canary assignments: maps a proposed version to a subset of slots,
-- with entry criteria and expected duration.
CREATE TABLE IF NOT EXISTS canary_assignments (
  id                  BIGSERIAL PRIMARY KEY,
  proposal_id         BIGINT NOT NULL REFERENCES tuning_proposals(id),
  canary_version_id   INTEGER NOT NULL REFERENCES config_versions(id),
  baseline_version_id INTEGER NOT NULL REFERENCES config_versions(id),
  slot_ids            INTEGER[] NOT NULL,
  started_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  ended_at            TIMESTAMPTZ,
  status              TEXT NOT NULL DEFAULT 'running'
                        CHECK (status IN ('running','passed','failed','aborted')),
  min_trades_required INTEGER NOT NULL,
  required_ci_bps     NUMERIC NOT NULL,
  result              JSONB,
  notes               TEXT
);
CREATE INDEX IF NOT EXISTS idx_canary_assignments_running
  ON canary_assignments (started_at DESC) WHERE status = 'running';
CREATE INDEX IF NOT EXISTS idx_canary_assignments_proposal
  ON canary_assignments (proposal_id);

-- Apply events: one row per global-apply transition.
CREATE TABLE IF NOT EXISTS apply_events (
  id                  BIGSERIAL PRIMARY KEY,
  ts                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  canary_id           BIGINT REFERENCES canary_assignments(id),
  from_version_id     INTEGER NOT NULL REFERENCES config_versions(id),
  to_version_id       INTEGER NOT NULL REFERENCES config_versions(id),
  applied_by          TEXT NOT NULL,
  rationale           TEXT NOT NULL
);

-- Rollback events: audit trail. Rolling back creates a new config_version
-- (source='rollback') that mirrors the last-known-good config and
-- references the bad version in rationale.
CREATE TABLE IF NOT EXISTS rollback_events (
  id                  BIGSERIAL PRIMARY KEY,
  ts                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  bad_version_id      INTEGER NOT NULL REFERENCES config_versions(id),
  rolled_back_to_id   INTEGER NOT NULL REFERENCES config_versions(id),
  trigger             TEXT NOT NULL,        -- 'pf_regression','dd_breach','frequency_anomaly','global_halt','manual'
  triggered_by        TEXT NOT NULL,
  evidence            JSONB
);

-- Meta-learner weekly reports.
CREATE TABLE IF NOT EXISTS optimizer_meta_reports (
  id                  BIGSERIAL PRIMARY KEY,
  iso_week            TEXT NOT NULL UNIQUE,
  generated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  report              JSONB NOT NULL,
  summary             TEXT
);

-- Per-source auto-apply flags. Default OFF per design principle: manual
-- approval mode until 60-day trust earned. Each source can flip independently.
CREATE TABLE IF NOT EXISTS optimizer_source_flags (
  source              TEXT PRIMARY KEY,
  auto_apply          BOOLEAN NOT NULL DEFAULT FALSE,
  enabled             BOOLEAN NOT NULL DEFAULT TRUE,
  updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_by          TEXT
);
INSERT INTO optimizer_source_flags (source, auto_apply, enabled, updated_by) VALUES
  ('numerical',       FALSE, TRUE,  'migration:033'),
  ('llm_failure',     FALSE, FALSE, 'migration:033'),
  ('llm_strategic',   FALSE, FALSE, 'migration:033'),
  ('llm_opportunity', FALSE, FALSE, 'migration:033')
ON CONFLICT (source) DO NOTHING;

-- Kill switch for the optimizer process itself. When FALSE, all generators
-- stop emitting proposals; canary monitors keep running so in-flight canaries
-- complete safely. Auto-rollback also keeps running.
INSERT INTO config (key, value, updated_by) VALUES
  ('OPTIMIZER_ENABLED', 'true'::jsonb, 'migration:033')
ON CONFLICT (key) DO NOTHING;

-- Retire the old auto-apply flag so it can't conflict with the new
-- per-source flags. Keeping the key but forcing it FALSE (and documenting
-- the supersession). The bot's auto_apply_pending_tuning becomes a no-op.
UPDATE config SET value = 'false'::jsonb, updated_by = 'migration:033'
  WHERE key = 'TUNING_AUTO_APPLY';

COMMIT;
