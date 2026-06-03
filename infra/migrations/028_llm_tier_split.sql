-- 028: PR10 — per-touchpoint LLM model routing.
--
-- Today every LLM call uses Opus 4.7. Classification-grade work (entry_veto)
-- is the highest-frequency touchpoint and doesn't benefit from Opus-level
-- reasoning — Haiku matches Opus accuracy on that surface at ~10× lower
-- cost and ~3× lower latency. Tiering also lets us keep Opus for stop_adjust
-- and exit_veto where a mistaken override is expensive.
--
-- Flag-gated by LLM_TIER_SPLIT_ENABLED (default FALSE). When false, the
-- code path in llm._model_for falls back to the caller's historical model
-- (OPUS_ONLINE or HAIKU) byte-for-byte.
--
-- llm_calls provides the observability substrate PR11 will build on: every
-- call records (touchpoint, model, tokens, latency, validation status).

BEGIN;

CREATE TABLE IF NOT EXISTS llm_calls (
  id              BIGSERIAL PRIMARY KEY,
  ts              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  call_purpose    TEXT NOT NULL,
  model           TEXT NOT NULL,
  tokens_in       INTEGER,
  tokens_out      INTEGER,
  latency_ms      INTEGER,
  response_valid  BOOLEAN,
  proposal_id     BIGINT,
  meta            JSONB
);
CREATE INDEX IF NOT EXISTS llm_calls_ts_idx ON llm_calls (ts DESC);
CREATE INDEX IF NOT EXISTS llm_calls_purpose_idx ON llm_calls (call_purpose, ts DESC);

INSERT INTO config (key, value, updated_by) VALUES
  ('LLM_TIER_SPLIT_ENABLED',   'false'::jsonb,                            'migration:028'),
  ('LLM_MODEL_REGIME',         '"anthropic/claude-opus-4.7"'::jsonb,      'migration:028'),
  ('LLM_MODEL_RANKING',        '"anthropic/claude-sonnet-4.6"'::jsonb,    'migration:028'),
  ('LLM_MODEL_VETO',           '"anthropic/claude-haiku-4.5"'::jsonb,     'migration:028'),
  ('LLM_MODEL_STOP_ADJUST',    '"anthropic/claude-opus-4.7"'::jsonb,      'migration:028'),
  ('LLM_MODEL_EXIT_VETO',      '"anthropic/claude-opus-4.7"'::jsonb,      'migration:028'),
  ('LLM_MODEL_NEWS',           '"anthropic/claude-sonnet-4.6"'::jsonb,    'migration:028')
ON CONFLICT (key) DO NOTHING;

COMMIT;
