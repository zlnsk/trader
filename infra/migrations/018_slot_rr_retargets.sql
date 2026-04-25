-- 018: PR1 — retarget slots with broken fee-adjusted R:R.
--
-- Prior to this migration, three slot tiers had net-expectancy problems
-- that no realistic win rate could overcome after fees+slippage:
--
--   slots 10-12 (intraday_safe)     target 0.5 / stop 0.7 → net R:R ≈ 0.45
--     Required win rate for break-even: ~69%. No realistic
--     mean-reversion system sustains that — see fees.net_expected_rr.
--
--   slots 19-20 (crypto_balanced)   target 1.5 / stop 1.0 → net R:R ≈ 0.84
--     Crypto round-trip fee is 0.36% (0.18% × 2) plus slippage, which
--     collapses 1.5% gross target to ~1.08% net while inflating the
--     stop side. Required WR ~54% minimum.
--
--   slot 21 (crypto_aggressive)     target 2.5 / stop 1.5 → net R:R 1.13
--     Marginal but works. Bumped to 2.8 for breathing room.
--
-- New values (validated by fees.net_expected_rr ≥ 0.6 floor, enforced at
-- startup from this migration onward):
--
--   slots 10-12: target 0.5 → 0.8   (R:R ≈ 0.79)
--   slots 19-20: target 1.5 → 2.2 / stop 1.0 → 1.3  (R:R ≈ 1.03)
--   slot 21:     target 2.5 → 2.8   (R:R ≈ 1.24)
--
-- Stop values for slots 10-12 unchanged (-0.7%); tightening further
-- would land inside MIN_STOP_WIDTH_PCT floor. Crypto stops widened
-- proportionally with target so R:R ratio holds at ~1.0.

BEGIN;

-- slots 10-12 intraday_safe — target 0.5 → 0.8 (stop unchanged at -0.7)
UPDATE slot_profiles
   SET target_profit_pct = 0.8,
       updated_at        = now()
 WHERE slot IN (10, 11, 12);

-- slots 19-20 crypto_balanced — target 1.5 → 2.2, stop 1.0 → 1.3
UPDATE slot_profiles
   SET target_profit_pct = 2.2,
       stop_loss_pct     = -1.3,
       updated_at        = now()
 WHERE slot IN (19, 20);

-- slot 21 crypto_aggressive — target 2.5 → 2.8 (stop unchanged at -1.5)
UPDATE slot_profiles
   SET target_profit_pct = 2.8,
       updated_at        = now()
 WHERE slot = 21;

COMMIT;
