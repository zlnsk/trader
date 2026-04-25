# Trader

Dip-buy stock trading bot against Interactive Brokers. Detects sharp price drops, buys with strict capital limits, sells on recovery. Claude (via OpenRouter) acts as qualitative veto over quantitative signals. Also runs an **Overnight Edge** strategy (MOC entry, MOO exit on dedicated slots 25-29).

**Live trading enabled.** Paper mode available via `TRADING_MODE` config.

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
