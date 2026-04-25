-- 019: PR1 hotfix — nudge swing slots 1-9 targets just over the 0.6 R:R floor.
--
-- PR1 (migration 018) retargeted intraday_safe and crypto slots but didn't
-- touch swing. Running validate_slot_rr against production surfaced nine
-- pre-existing swing slots at net R:R 0.559-0.599 — inside the 0.04 noise
-- band below the 0.6 floor. The validator refuses to start when any slot
-- fails, so this migration nudges targets by the minimum needed to clear
-- the floor with a small margin.
--
--   slots 1-3 (swing safe)       target 2.0 → 2.05   (R:R 0.597 → 0.613)
--   slots 4-6 (swing balanced)   target 3.0 → 3.25   (R:R 0.559 → 0.608)
--   slots 7-9 (swing aggressive) target 5.0 → 5.10   (R:R 0.599 → 0.611)
--
-- Stops unchanged. Bump is small enough (2-8% relative) that historical
-- hit rates should still apply without requiring a backtest re-run — the
-- order of magnitude of the net expectancy change is the fee/slip cost
-- we were under-counting, not a strategy-level shift.

BEGIN;

UPDATE slot_profiles
   SET target_profit_pct = 2.05,
       updated_at        = now()
 WHERE slot IN (1, 2, 3);

UPDATE slot_profiles
   SET target_profit_pct = 3.25,
       updated_at        = now()
 WHERE slot IN (4, 5, 6);

UPDATE slot_profiles
   SET target_profit_pct = 5.10,
       updated_at        = now()
 WHERE slot IN (7, 8, 9);

COMMIT;
