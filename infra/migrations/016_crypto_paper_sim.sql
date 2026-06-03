-- 016: seed crypto shadow-sim config.
--
-- IBKR paper accounts do not support Paxos crypto trading (orders silently
-- flip to Inactive on submit). The bot's broker layer now synthesizes crypto
-- fills at live prices ± slippage when CRYPTO_PAPER_SIM is true. Flipping
-- CRYPTO_PAPER_SIM=false (or deleting the row) routes crypto orders through
-- ib.placeOrder again — intended for use on a live-funded account with the
-- Cryptocurrencies permission enabled.

BEGIN;

INSERT INTO config (key, value, updated_by) VALUES
  ('CRYPTO_PAPER_SIM',              'true'::jsonb, 'migration:016'),
  ('CRYPTO_PAPER_SIM_SLIPPAGE_BPS', '3'::jsonb,    'migration:016')
ON CONFLICT (key) DO NOTHING;

COMMIT;
