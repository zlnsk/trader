"""Hard-coded safety limits.

EVERYTHING in this file is a CODE-LEVEL CONSTANT, deliberately NOT in
the config table and NOT tunable by the optimizer itself. Per design
principle #6: the optimizer cannot tune the limits that constrain the
optimizer.

Change a value here only via an explicit commit + code review — never
via runtime config.
"""
from __future__ import annotations

# ── Sample-size gates ────────────────────────────────────────────────
# No decision rests on fewer than this many trades of evidence. Applied
# at every metric-consuming path: validator, anomaly detector, meta.
MIN_N_SAMPLES = 50

# Canary must observe at least this many fills before passing.
MIN_CANARY_TRADES = 30

# Bootstrap resamples in the validator.
BOOTSTRAP_SAMPLES = 2000

# Max slots a canary may occupy, ever. Prevents one bad global
# parameter from sweeping the whole book.
MAX_CANARY_SLOT_FRACTION = 0.3     # 30% of total slots
MAX_CANARY_SLOTS_ABSOLUTE = 3      # hard ceiling, ignores fraction

# ── Parameter-change caps per proposal ───────────────────────────────
# A single proposal may not move any parameter by more than this %
# relative to its active value. Multiple proposals over time can walk a
# parameter arbitrarily far; cumulative-drift detector in anomaly.py
# flags that separately.
MAX_SINGLE_CHANGE_PCT = 15.0

# ── Cool-down windows ────────────────────────────────────────────────
# After a rollback, reject any proposal on the rolled-back parameter for
# this long. Stops ping-pong loops.
POST_ROLLBACK_COOLDOWN_HOURS = 48

# ── Regression triggers (auto-rollback) ──────────────────────────────
# Rolling-window thresholds. Breaching any triggers auto-rollback.
# All measured over the most recent ROLLBACK_WINDOW_DAYS of trades.
ROLLBACK_WINDOW_DAYS = 7
ROLLBACK_PF_DROP = 0.25            # profit factor drops >=25% vs baseline
ROLLBACK_DD_BREACH_PCT = 5.0       # drawdown > 5% over window
ROLLBACK_FREQ_COLLAPSE_PCT = 60.0  # trade count drops by >=60%

# ── Complexity penalty ───────────────────────────────────────────────
# Every extra tuned parameter in a proposal adds this bps to the
# improvement bar it must clear. Guards against overfitting via
# many-param jiggling.
COMPLEXITY_PENALTY_BPS_PER_PARAM = 5.0

# ── Hard-excluded keys ───────────────────────────────────────────────
# Keys that MAY NEVER be tuned by the optimizer, even if some
# config_managed_keys row lists them. Belt-and-braces.
FORBIDDEN_TUNE_KEYS = frozenset({
    "BOT_ENABLED",
    "TRADING_MODE",
    "UNIVERSE",
    "MAX_SLOTS",
    "OPTIMIZER_ENABLED",
    "TUNING_AUTO_APPLY",          # deprecated but belt-and-braces
    "CRYPTO_PAPER_SIM",
    "NEWS_WATCHER_ENABLED",
})

# Structural-change keys: may be proposed, but MUST route through
# human-approval regardless of source auto_apply flag. Per design #7.
STRUCTURAL_KEYS = frozenset({
    "SLOT_SIZE_EUR",
    "LLM_MODEL_VETO",
    "LLM_MODEL_REGIME",
    "LLM_MODEL_RANKING",
    "LLM_MODEL_STOP_ADJUST",
    "LLM_MODEL_EXIT_VETO",
    "LLM_MODEL_NEWS",
})

# ── LLM-cost guards ──────────────────────────────────────────────────
# Optimizer's OWN LLM budget, separate from the bot's. If exceeded,
# LLM hypothesis generators stop emitting until next day.
OPTIMIZER_DAILY_LLM_USD_BUDGET = 5.0
