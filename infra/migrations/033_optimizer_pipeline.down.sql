BEGIN;
DROP TABLE IF EXISTS optimizer_source_flags;
DROP TABLE IF EXISTS optimizer_meta_reports;
DROP TABLE IF EXISTS rollback_events;
DROP TABLE IF EXISTS apply_events;
DROP TABLE IF EXISTS canary_assignments;
DROP TABLE IF EXISTS optimizer_findings;
ALTER TABLE config_versions DROP CONSTRAINT IF EXISTS fk_config_versions_proposal;
ALTER TABLE tuning_proposals DROP CONSTRAINT IF EXISTS tuning_proposals_status_check;
ALTER TABLE tuning_proposals ADD CONSTRAINT tuning_proposals_status_check
  CHECK (status = ANY (ARRAY['pending','approved','rejected','applied']));
ALTER TABLE tuning_proposals
  DROP COLUMN IF EXISTS rolled_back_at,
  DROP COLUMN IF EXISTS applied_version_id,
  DROP COLUMN IF EXISTS canary_id,
  DROP COLUMN IF EXISTS adversary_ts,
  DROP COLUMN IF EXISTS adversary_result,
  DROP COLUMN IF EXISTS finding_id,
  DROP COLUMN IF EXISTS source;
COMMIT;
