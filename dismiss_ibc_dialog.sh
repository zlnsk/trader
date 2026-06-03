#!/bin/bash
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
