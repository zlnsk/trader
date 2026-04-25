#!/usr/bin/env bash
# Trader bot heartbeat watchdog.
#
# Queries the 'heartbeat' table; if the most-recent row is older than
# STALE_AFTER_SEC, SIGTERMs trader-bot so systemd can restart it. Solves the
# 2026-04-21 4-hour hang: ib_insync's internal keepalive did not detect a
# stalled socket, so the tick loop slept indefinitely and missed the
# 21:00-UTC daily_reports write.
#
# Runs from trader-watchdog.timer every 2 minutes. Exits 0 either way so the
# timer stays green; failures to reach the DB are logged but do not cascade.

set -euo pipefail

STALE_AFTER_SEC=${STALE_AFTER_SEC:-300}

# Heartbeat is written by the bot's main loop on every tick (a few seconds).
# 5 minutes is ~60-150 ticks of slack — comfortably past transient IB
# reconnects (~60-90s) but well short of the 4h hang we are guarding against.
age=$(su - postgres -c \
    "psql -d trading -tAXc \"SELECT EXTRACT(EPOCH FROM (NOW() - ts))::int FROM heartbeat ORDER BY ts DESC LIMIT 1\"" \
    2>/dev/null || true)

if [[ -z "${age}" ]]; then
    logger -t trader-watchdog "heartbeat query returned no rows; skipping"
    exit 0
fi

if [[ "${age}" =~ ^[0-9]+$ ]] && (( age > STALE_AFTER_SEC )); then
    logger -t trader-watchdog "heartbeat stale (${age}s > ${STALE_AFTER_SEC}s); restarting trader-bot"
    systemctl restart trader-bot.service || true
else
    :
fi

exit 0
