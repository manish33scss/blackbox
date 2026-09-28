#!/bin/bash
# Deploys Blackbox to the Luckfox board.
# Precondition: board reachable at BOARD_IP (network bring-up is handled
# separately -- see view_blackbox.sh / the manual `nmcli`/`ip addr` steps
# used throughout development). Run from the repo root or scripts/.
set -euo pipefail

BOARD_IP="${BOARD_IP:-172.32.0.93}"
BOARD_USER="root"
SSH_KEY="$HOME/.ssh/luckfox_pico"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

ssh_cmd() {
  SSH_AUTH_SOCK= ssh -o IdentitiesOnly=yes -i "$SSH_KEY" "${BOARD_USER}@${BOARD_IP}" "$@"
}
scp_cmd() {
  SSH_AUTH_SOCK= scp -o IdentitiesOnly=yes -i "$SSH_KEY" "$@"
}

echo "==> Checking board is reachable at $BOARD_IP..."
ssh_cmd "echo ok" >/dev/null

echo "==> Creating /root/blackbox on board..."
ssh_cmd "mkdir -p /root/blackbox"

echo "==> Copying application code..."
scp_cmd "$REPO_DIR"/device/*.py "$REPO_DIR/device/configure_rkipc.sh" "${BOARD_USER}@${BOARD_IP}:/root/blackbox/"

echo "==> Copying main.py entrypoint (S99python auto-runs /root/main.py)..."
scp_cmd "$REPO_DIR/device/main.py" "${BOARD_USER}@${BOARD_IP}:/root/main.py"

# S19 so the big store is mounted before S21appinit (RkLunch.sh) sets up the
# camera stack. Replaces the older S98 install.
echo "==> Installing storage mount init script as S19datapartition..."
scp_cmd "$REPO_DIR/device/setup_data_partition.sh" "${BOARD_USER}@${BOARD_IP}:/etc/init.d/S19datapartition"
ssh_cmd "chmod +x /etc/init.d/S19datapartition && rm -f /etc/init.d/S98datapartition"

echo "==> Configuring rkipc (TS recording, snapshots, no autostart)..."
ssh_cmd "sh /root/blackbox/configure_rkipc.sh"

echo "==> Removing leftovers from earlier versions..."
ssh_cmd "rm -rf /usr/lib/python3.11/site-packages/flask /root/blackbox/templates /root/blackbox/thumbnail.py"

echo "==> Verifying import on-device (stdlib only now, should be near-instant)..."
ssh_cmd "python3 -c 'import sys, time; sys.path.insert(0, \"/root/blackbox\"); t=time.time(); import app; print(\"import OK,\", time.time()-t, \"s\")'"

# main.py detaches itself, so no '&'. Use killall (matches the process name),
# not `ps | grep main.py` -- that also matches the ssh shell running the command.
echo "==> Done. Restart the board (or run: ssh -i $SSH_KEY ${BOARD_USER}@${BOARD_IP} 'killall python3; sleep 1; python3 /root/main.py') to pick up the new code."
