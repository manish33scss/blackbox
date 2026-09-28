#!/bin/bash
# One-command launcher: bring up networking to the Luckfox over USB, then
#   view_blackbox.sh        open the Blackbox web UI in the browser
#   view_blackbox.sh live   start the camera if it's off and open the live
#                           view in VLC (browsers can't play RTSP)
# The board's USB-Ethernet gadget gets a new MAC/interface name on every
# boot, so this re-detects the interface rather than assuming a fixed name.
set -euo pipefail

MODE="${1:-ui}"
BOARD_IP="172.32.0.93"
HOST_IP="172.32.0.1/24"
UI="http://${BOARD_IP}:5000"
LIVE="rtsp://${BOARD_IP}/live/0"

bring_up_network() {
  echo "==> Looking for the Luckfox's USB network interface..."
  IFACE=""
  for i in 1 2 3 4 5; do
    IFACE=$(ip -o link show | awk -F': ' '/enx[0-9a-f]+:/ {print $2; exit}')
    [ -n "$IFACE" ] && break
    sleep 1
  done
  if [ -z "$IFACE" ]; then
    echo "No enx* interface found. Is the board plugged in over USB?"
    exit 1
  fi
  echo "==> Found interface: $IFACE"

  CONN=$(nmcli -g GENERAL.CONNECTION device show "$IFACE" 2>/dev/null || true)
  if [ -z "$CONN" ] || [ "$CONN" = "--" ]; then
    nmcli device connect "$IFACE" >/dev/null 2>&1 || true
    sleep 1
    CONN=$(nmcli -g GENERAL.CONNECTION device show "$IFACE" 2>/dev/null || true)
  fi
  if [ -z "$CONN" ] || [ "$CONN" = "--" ]; then
    echo "Could not find/create a NetworkManager connection for $IFACE."
    exit 1
  fi

  echo "==> Assigning static IP $HOST_IP to $IFACE (profile: $CONN)..."
  nmcli connection modify "$CONN" ipv4.method manual ipv4.addresses "$HOST_IP"
  nmcli connection up "$CONN" >/dev/null

  echo "==> Waiting for the board to respond at $BOARD_IP..."
  for i in $(seq 1 20); do
    ping -c1 -W1 "$BOARD_IP" >/dev/null 2>&1 && return 0
    sleep 1
  done
  echo "Board didn't respond at $BOARD_IP after 20s. It may still be booting -- try again in a few seconds."
  exit 1
}

rtsp_up() { timeout 2 bash -c "echo > /dev/tcp/${BOARD_IP}/554" 2>/dev/null; }

if ping -c1 -W1 "$BOARD_IP" >/dev/null 2>&1; then
  echo "==> Board already reachable at $BOARD_IP."
else
  bring_up_network
fi

if [ "$MODE" = "live" ]; then
  if ! rtsp_up; then
    echo "==> Camera is off; starting it..."
    curl -s -o /dev/null -X POST "$UI/camera/start"
    for i in $(seq 1 30); do rtsp_up && break; sleep 1; done
    rtsp_up || { echo "Camera didn't come up within 30s. Check the web UI at $UI"; exit 1; }
  fi
  echo "==> Opening live view: $LIVE"
  if command -v vlc >/dev/null; then
    vlc --network-caching=300 "$LIVE" >/dev/null 2>&1 &
  else
    ffplay -fflags nobuffer -rtsp_transport tcp "$LIVE" >/dev/null 2>&1 &
  fi
else
  echo "==> Opening $UI"
  xdg-open "$UI" >/dev/null 2>&1 &
fi
