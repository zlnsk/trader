# Trader

Dip-buy stock trading bot against Interactive Brokers. Detects sharp price drops, buys with strict capital limits, sells on recovery. Claude (via OpenRouter) acts as qualitative veto over quantitative signals. Also runs an **Overnight Edge** strategy (MOC entry, MOO exit on dedicated slots 25-29).

**Paper mode by default.** Real money is one config flip away — see *Trading mode* below before flipping.

## Layout

```
./
├── bot/          Python bot — signals, IBKR order lifecycle, LLM veto, overnight
├── dashboard/    Next.js 15 standalone app at trader.example.com
├── optimizer/    Python scheduler for parameter tuning
├── backtests/    Historical backtest results
├── scripts/      Utility scripts
└── infra/
    ├── docker-compose.yml  IB Gateway container
    ├── ib-gateway.env      IBKR creds (gitignored, mode 600)
    ├── .env                Shared env (DATABASE_URL, OpenRouter key — gitignored)
    └── migrations/         Postgres DDL
```

## Host

- **CT 108 `trader`** on host, Debian 13, 2 GB RAM, 20 GB disk.
- Postgres 17 bare install, DB `trading`, role `trader`.
- Dashboard: Next.js 15 standalone, port 3000 bound 0.0.0.0 (reverse-proxied by Caddy).
- Bot: `systemctl {start,stop,status} trader-bot`.
- Optimizer: `systemctl {start,stop,status} trader-optimizer` (or runs under same unit if combined).
- IB Gateway: `cd ./infra && docker compose up -d` (running).

## Services

| Service | Runtime | Path |
|---------|---------|------|
| Bot | systemd / manual | `./bot/.venv/bin/python -m bot.main` |
| Optimizer | systemd / manual | `./optimizer/.venv/bin/python -m optimizer.scheduler` |
| Dashboard | Node.js standalone | `cd ./dashboard && npm run build && npm start` |
| IB Gateway | Docker Compose | `./infra/docker-compose.yml` |
| Postgres | systemd | `postgres` service on default port |

## Edge path

`trader.example.com` → Pangolin SSO → newt → LXC 106 Caddy `@trader` → `10.0.0.10:3000` with `X-Proxy-Secret` (verified by Next middleware).

## Kill switch

`config.BOT_ENABLED` (jsonb bool) in the `trading` DB. The bot polls this every 10 s. Dashboard exposes `/api/kill-switch` to toggle. Disabling halts new buys immediately; open positions continue to be monitored for exit.

## Strategies

- **Mean-reversion** (primary): dip-buy on slots 1-3 with RSI/SMA20 distance, LLM veto, and fee-aware sizing.
- **Overnight Edge** (slots 25-29): MOC BUY at close, MOO SELL at next open. Independent scan windows (15:45 / 09:25 ET).

## Schema

`config`, `signals`, `positions`, `orders`, `audit_log`, `heartbeat`, `daily_reports`, `tuning_proposals`.

## Key files

- `bot/bot/main.py` — main tick loop, reconciliation, shutdown handling
- `bot/bot/strategies/overnight.py` — overnight strategy (MOC/MOO lifecycle)
- `bot/bot/broker.py` — IBKR market data and order placement
- `bot/bot/strategy.py` — mean-reversion signal and position management
- `bot/bot/risk.py` — circuit breakers and auto-kill
- `bot/bot/jobs.py` — scheduled reports and tuning

## Deploy

Assumes Debian/Ubuntu with Python 3.11+, Node 22+, Postgres 17+, and Docker.

```bash
# 1) Postgres + schema
sudo -u postgres psql -c "CREATE ROLE trader LOGIN PASSWORD '<pg-pass>';"
sudo -u postgres psql -c "CREATE DATABASE trading OWNER trader;"
for f in infra/migrations/*.sql; do
  sudo -u postgres psql -d trading -f "$f"
done

# 2) Python venvs
( cd bot       && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt )
( cd optimizer && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt )

# 3) Dashboard
( cd dashboard && npm install && npm run build )

# 4) Configure
cp infra/.env.example            infra/.env
cp infra/ib-gateway.env.example  infra/ib-gateway.env
chmod 600 infra/.env infra/ib-gateway.env
# fill in the values (see tables below), then:

# 5) Run
( cd infra      && docker compose up -d ib-gateway )
( cd bot        && .venv/bin/python -m bot.main )            # bot
( cd optimizer  && .venv/bin/python -m optimizer.scheduler ) # optimizer (separate process)
( cd dashboard  && npm start )                                # dashboard
```

### `infra/.env` — required

| Var | Required | Purpose |
|---|---|---|
| `DATABASE_URL` | yes | `postgresql://trader:<pg-pass>@localhost/trading` |
| `OPENROUTER_API_KEY` | yes | LLM veto, regime, news watch, optimizer reasoning. Get one at https://openrouter.ai. |
| `PROXY_SECRET` | yes | 32-byte base64; verified by Next.js dashboard middleware. Share between dashboard and your reverse proxy. |
| `OPENROUTER_MODEL` | optional | Fallback model when per-touchpoint config in DB is unset (default `anthropic/claude-haiku-4.5`). |
| `LLM_DAILY_BUDGET_USD` | optional | Daily cap on combined LLM spend; bot abstains when exceeded (default 20). |
| `MANUAL_APPROVAL_MODE` | optional | When `true`, every entry queues into `pending_approvals` for dashboard approve. Recommended for first day live. |
| `SMTP_HOST/PORT/USER/PASSWORD/FROM/TO` + `NOTIFY_ENABLED` | optional | Email notifications for fills, daily summary, critical findings, news-watch high-severity. |

### `infra/ib-gateway.env` — required

| Var | Purpose |
|---|---|
| `TWS_USERID`, `TWS_PASSWORD` | IBKR account credentials (paper or live — match `TRADING_MODE`). |
| `TRADING_MODE` | `paper` or `live`. **Always start with `paper`.** |

## Trading mode

`TRADING_MODE=paper` runs against IBKR's simulated environment with real-time market data and simulated fills. **Use it until you've validated at least 2 weeks of paper behaviour** — the bot's `Fill quality` dashboard card surfaces `paper_optimism_eur` (the gap between paper fills and a half-spread-penalised shadow fill). If optimism > 50% of net realized P&L, the strategy isn't actually profitable net of real execution. Don't flip.

To go live:

1. Verify paper P&L vs `paper_optimism_eur` as above.
2. Set `MANUAL_APPROVAL_MODE=true` in the `config` table for the first day.
3. Reduce `SLOT_SIZE_EUR` to a small value (e.g. 100) for the first day.
4. In both `infra/.env` and `infra/ib-gateway.env`, set `TRADING_MODE=live`. Update `IB_PORT` if your container exposes the live port differently (gnzsnz/ib-gateway maps live to internal 4003).
5. `docker compose up -d --force-recreate ib-gateway`. Handle 2FA on the IBKR mobile app on first launch.
6. Restart `trader-bot` and `trader-optimizer`.
7. Watch the first few fills. Walk away from the terminal exactly once.

## Suggestions

- **Cheap LLM tier works.** Per-touchpoint model split lives in the `config` table (`LLM_MODEL_VETO`, `LLM_MODEL_REGIME`, `LLM_MODEL_RANKING`, `LLM_MODEL_STOP_ADJUST`, `LLM_MODEL_EXIT_VETO`, `LLM_MODEL_NEWS`). Sane defaults: `claude-haiku-4.5` for the high-frequency enums (entry/exit/stop veto), `perplexity/sonar` for news watch (real grounded search at $1/$1 per 1M tokens), `claude-opus-4.7` only for the rare `market_regime` call. Total LLM spend lands ~$2–7/day during active US+EU trading. See `bot/bot/cost.py` for current pricing.
- **Paper before live**: 4–8 weeks is reasonable. Live equity is irreversible; paper drift teaches you what your real edge actually is.
- **Kill switch is a click**: `BOT_ENABLED=false` in `config` halts entries within 10 s. Keep the dashboard handy.
- **Auto-tuning bypasses the canary** today. The `TUNING_AUTO_APPLY=true` path in `bot/bot/jobs.py:auto_apply_pending_tuning` writes whitelisted threshold proposals straight to `config` (no 7-gate adversary, no canary). Set it to `false` if you want every change reviewed by hand on the dashboard.
- **Backtests live in `backtests/`** — run them before changing slot R:R targets or stop widths.
- **Notifications**: SMTP path is opt-in. If you wire it up you'll get a mail per fill, a daily summary, plus immediate alerts on circuit-breaker, auto-kill, and `severity=critical` optimizer findings. The bot polls `optimizer_findings` cursor each tick so each finding mails at most once.
- **Self-supervised**: this is not a managed product. You can lose real money. Don't run live against capital you can't afford to lose.
