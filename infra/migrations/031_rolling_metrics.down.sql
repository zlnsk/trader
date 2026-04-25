BEGIN;
DROP TABLE IF EXISTS metrics_refresh_state;
DROP TABLE IF EXISTS metrics_llm_rolling;
DROP TABLE IF EXISTS metrics_symbol_rolling;
DROP TABLE IF EXISTS metrics_tod_rolling;
DROP TABLE IF EXISTS metrics_regime_rolling;
DROP TABLE IF EXISTS metrics_slot_rolling;
COMMIT;
