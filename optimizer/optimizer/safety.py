"""Hard-coded safety limits.

EVERYTHING in this file is a CODE-LEVEL CONSTANT, deliberately NOT in
the config table and NOT tunable by the optimizer itself. Per design
principle #6: the optimizer cannot tune the limits that constrain the
optimizer.

Change a value here only via an explicit commit + code review — never
via runtime config.
"""
from __future__ import annotations




MIN_N_SAMPLES = 50


MIN_CANARY_TRADES = 30


BOOTSTRAP_SAMPLES = 2000



MAX_CANARY_SLOT_FRACTION = 0.3
MAX_CANARY_SLOTS_ABSOLUTE = 3






MAX_SINGLE_CHANGE_PCT = 15.0




POST_ROLLBACK_COOLDOWN_HOURS = 48




ROLLBACK_WINDOW_DAYS = 7
ROLLBACK_PF_DROP = 0.25
ROLLBACK_DD_BREACH_PCT = 5.0
ROLLBACK_FREQ_COLLAPSE_PCT = 60.0





COMPLEXITY_PENALTY_BPS_PER_PARAM = 5.0




FORBIDDEN_TUNE_KEYS = frozenset({
    "BOT_ENABLED",
    "TRADING_MODE",
    "UNIVERSE",
    "MAX_SLOTS",
    "OPTIMIZER_ENABLED",
    "TUNING_AUTO_APPLY",
    "CRYPTO_PAPER_SIM",
    "NEWS_WATCHER_ENABLED",
})



STRUCTURAL_KEYS = frozenset({
    "SLOT_SIZE_EUR",
    "LLM_MODEL_VETO",
    "LLM_MODEL_REGIME",
    "LLM_MODEL_RANKING",
    "LLM_MODEL_STOP_ADJUST",
    "LLM_MODEL_EXIT_VETO",
    "LLM_MODEL_NEWS",
})




OPTIMIZER_DAILY_LLM_USD_BUDGET = 5.0
