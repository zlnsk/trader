#!/bin/bash
# Dismisses the "Real paper-trading not acknowledged" dialog in IB Gateway.
# Root cause: IBC issue #101 — AcceptNonBrokerageAccountWarning=yes detects the
# dialog on Linux but the button-click fails silently. We click it via xdotool.
# Runs inside the trader-ib-gateway container; retries for 2min after Gateway up.
set -e
CONTAINER="${CONTAINER:-trader-ib-gateway}"
for i in $(seq 1 24); do
  WID=$(docker exec --user root "$CONTAINER" sh -c 'DISPLAY=:1 xdotool search --name "^Warning$" 2>/dev/null | head -1' || true)
  if [ -n "$WID" ]; then
    docker exec --user root "$CONTAINER" sh -c "DISPLAY=:1 xdotool mousemove 511 485 click 1" || true
    sleep 2
    STILL=$(docker exec --user root "$CONTAINER" sh -c 'DISPLAY=:1 xdotool search --name "^Warning$" 2>/dev/null | head -1' || true)
    if [ -z "$STILL" ]; then echo "dismissed dialog $WID"; exit 0; fi
  fi
  sleep 5
done
echo "no dialog found after 2min"; exit 0
