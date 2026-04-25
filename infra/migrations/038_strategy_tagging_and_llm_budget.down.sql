-- 038 down: reverse the strategy tagging + LLM budget additions.

BEGIN;

DROP TABLE IF EXISTS llm_budget_per_strategy;

ALTER TABLE llm_calls DROP COLUMN IF EXISTS strategy;
ALTER TABLE llm_spend DROP COLUMN IF EXISTS strategy;

ALTER TABLE stop_adjust_decisions DROP COLUMN IF EXISTS strategy;

DROP INDEX IF EXISTS orders_strategy_idx;
ALTER TABLE orders DROP COLUMN IF EXISTS strategy;

DELETE FROM config WHERE key IN (
    'MOC_WINDOW_MIN_MINUTES_USD', 'MOC_WINDOW_MAX_MINUTES_USD',
    'MOC_WINDOW_MIN_MINUTES_EU',  'MOC_WINDOW_MAX_MINUTES_EU',
    'LLM_HTTP_TIMEOUT_SEC'
);

COMMIT;
