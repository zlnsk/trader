# trader-optimizer

Self-optimization service for the trader. Runs as a **separate process**
from `trader-bot`. Communicates via the shared Postgres DB only. A
crash here cannot affect trading.

## Design principles (non-negotiable)

1. Separate process. DB-only comms.
2. Every change passes through the full pipeline: observe → hypothesise
   → validate → canary → apply → monitor → rollback. No shortcut path.
3. Burden of proof on the proposal. Stasis is the default.
4. Sample-size gates everywhere (`MIN_N_SAMPLES = 50`).
5. Cadences keyed to trade-arrival rate, not calendar.
6. Hard safety limits live in `safety.py` — the optimizer cannot tune
   its own constraints.
7. Every applied change carries a human-readable rationale; a single
   `trace_lineage()` call reconstructs the full causal chain.
8. Structural decisions require human approval (dashboard).
9. Adversary first. Proposal generators only run once the validator is
   trusted.

## Layout

```
optimizer/
├── pyproject.toml
├── README.md
├── optimizer/
│   ├── __init__.py
│   ├── safety.py                # hard-coded limits; NEVER in config
│   ├── db.py                    # asyncpg pool + jsonb codec
│   ├── llm.py                   # OpenRouter client (self-contained)
│   ├── scheduler.py             # main long-running process
│   ├── metrics/
│   │   ├── definitions.py       # metric formulas + DEFN_VERSION
│   │   └── refresh.py           # incremental rolling-metric refresh
│   ├── config_store/
│   │   └── versions.py          # versioned config CRUD + lineage walk
│   ├── validator/
│   │   ├── adversary.py         # 7-gate rejection pipeline
│   │   ├── bootstrap.py         # two-sample bootstrap CI
│   │   └── replay.py            # signal-snapshot replay engine
│   ├── hypothesis/
│   │   ├── numerical.py         # TPE over bounded ranges (Optuna)
│   │   ├── llm_failure.py       # LLM failure-cluster reasoning
│   │   ├── llm_strategic.py     # weekly strategic review
│   │   └── llm_opportunity.py   # winning-cluster opportunity
│   ├── canary/
│   │   └── runner.py            # slot-scoped deployment + verdict
│   ├── lifecycle/
│   │   ├── apply.py             # canary → global
│   │   └── rollback.py          # auto + manual rollback
│   ├── anomaly/
│   │   ├── detector.py          # stat-rule detectors (DD, PF, freq, data-qa)
│   │   └── drift.py             # cumulative parameter drift
│   └── meta/
│       └── report.py            # weekly meta-learner report
└── tests/
    ├── test_metrics_definitions.py
    ├── test_metrics_refresh_integration.py
    ├── test_config_store.py
    ├── test_validator_unit.py
    ├── test_validator_integration.py
    ├── test_numerical_integration.py
    ├── test_canary_apply_rollback.py
    ├── test_scheduler_anomaly.py
    ├── test_llm_sources.py
    ├── test_meta.py
    └── test_safety_guards.py
```

## Schema (migrations 030-034)

- `030` — `features_dictionary`, `trade_outcomes`
- `031` — rolling-metric tables + `metrics_refresh_state`
- `032` — `config_versions`, `config_values`, `config_managed_keys`
- `033` — `optimizer_findings`, `canary_assignments`, `apply_events`,
  `rollback_events`, `optimizer_meta_reports`, `optimizer_source_flags`;
  extends `tuning_proposals`; retires `TUNING_AUTO_APPLY`
- `034` — trigger that writes `trade_outcomes` on position close

## Runbook

### Day-to-day monitoring

1. `GET /api/optimizer/state` — current active version, pending
   proposals, running canaries, recent findings.
2. `GET /api/optimizer/findings?unresolved=true` — things needing
   attention.
3. `GET /api/optimizer/meta` — weekly report.

### Approving a validated proposal

`POST /api/optimizer/proposals` with `{id: N, action: "approve"}`.
The scheduler picks it up on the next canary cadence.

### Rejecting a proposal

`POST /api/optimizer/proposals` with `{id: N, action: "reject"}`.

### Aborting a running canary

`POST /api/optimizer/canaries` with `{id: N, action: "abort"}`.

### Manual rollback (break-glass)

`POST /api/optimizer/override` with `{action: "force_rollback"}`.
The scheduler honours it on the next rollback-check tick (≤5 min).

### Stopping the optimizer entirely

`POST /api/optimizer/override` with `{action: "disable_optimizer"}`.
Running canaries continue; no new proposals; auto-rollback still
armed. Re-enable via `{action: "enable_optimizer"}`.

### Enabling a source (after 60-day trust build)

```bash
curl -X POST .../api/optimizer/override \
  -d '{"action":"set_source_flag","source":"numerical","auto_apply":true}'
```

### When the scheduler itself is broken at 3 am

1. `ssh host 'pct exec 108 -- systemctl stop trader-optimizer'`
   — trader-bot unaffected.
2. Check `journalctl -u trader-optimizer -n 200` for the traceback.
3. If a specific job is failing, disable just that job: delete the
   source flag row or set `enabled=false`.
4. Last-resort full cleanup: `DELETE FROM canary_assignments WHERE
   status='running'` + `UPDATE config_versions SET deactivated_at=NOW()
   WHERE scope->>'kind'='slots' AND deactivated_at IS NULL`.
   After this, bot reverts to global baseline.

### LLM cost watch

`SELECT date_trunc('day', ts), SUM(cost_usd) FROM llm_spend
 WHERE touchpoint LIKE 'optimizer:%' GROUP BY 1 ORDER BY 1 DESC LIMIT 7;`

Daily cap: `safety.OPTIMIZER_DAILY_LLM_USD_BUDGET = $5`. Exceeding it
makes LLM generators no-op until midnight UTC.

## Deploy

```bash
# On host:
pct exec 108 -- bash -c '
  cd . &&
  git pull &&
  python3 -m venv ./optimizer/.venv &&
  ./optimizer/.venv/bin/pip install \
    asyncpg httpx numpy scipy optuna python-dotenv pydantic
'
# Apply migrations
pct exec 108 -- su - postgres -c '
  for f in ./infra/migrations/{030,031,032,033,034}*.sql; do
    psql -d trading -v ON_ERROR_STOP=1 -f "$f";
  done
'
# Install unit + start
pct exec 108 -- install -m644 \
  ./infra/systemd/trader-optimizer.service \
  /etc/systemd/system/
pct exec 108 -- systemctl daemon-reload
pct exec 108 -- systemctl enable --now trader-optimizer
```

## Acceptance

Running with `auto_apply=false` on every source flag (manual-approval
mode), the service should:

- stay up continuously
- produce findings reflecting reality
- generate proposals from all three sources when conditions warrant
- correctly reject bad proposals at the validator stage (integration
  tests include this)
- correctly identify wins/regressions at the canary stage
- roll back promptly when regression is detected
- have zero impact on trader P&L (nothing applies without human click)

After 60 days of trust-building in manual-approval mode, flip individual
source flags to `auto_apply=true` via `/api/optimizer/override`.
