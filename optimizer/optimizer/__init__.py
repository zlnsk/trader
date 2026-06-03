"""Self-optimization service for the trader.

Runs as a separate process (trader-optimizer.service). Communicates with
the trader only via the shared Postgres DB. Crashes here do not affect
trading.

Package layout:
  metrics/       — rolling-metric refresh jobs + definitions
  config_store/  — versioned configuration CRUD
  validator/     — the adversary: rejects proposals that cannot prove themselves
  hypothesis/    — proposal generators (numerical + 3 LLM sources)
  anomaly/       — degradation and anomaly detection
  canary/        — partial-subset deployment
  lifecycle/     — apply + rollback
  meta/          — weekly meta-learner report
  safety.py      — hard-coded limits the optimizer cannot tune
  scheduler.py   — the long-running process, per-component cadences
"""
__version__ = "0.1.0"
