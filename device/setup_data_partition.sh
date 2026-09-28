#!/bin/sh
# Mounts /mnt/blackbox_data from the SD card's trailing unpartitioned space (~22.9GB
# on a 32GB card) via a fixed-offset loop device, since the card's
# partition table is Rockchip's proprietary format -- not a standard
# MBR/GPT that can be safely resized with normal tools. This deliberately
# never touches anything before this offset, where the Rockchip boot
# data (env/idblock/uboot/boot/oem/userdata/rootfs) lives.
#
# Offset derived from this firmware build's sd_update.txt: rootfs starts
# at sector 0x190640 (1,640,000) with allocated size 0xC00000 sectors
# (12,582,912) -> ends at sector 14,222,912. Rounded up to the next 1MB
# alignment boundary (2048-sector multiple) = sector 14,223,360.
#
# Deployed to the board as /etc/init.d/S19datapartition by deploy.sh (S19 so
# /mnt/blackbox_data is mounted before S21appinit, which rkipc depends on) --
# this file is NOT executed here, it's just source under version control.

DATA_START_SECTOR=14223360

case "$1" in
start)
  # Runs early in boot, so wait (bounded) for the SD card's device node
  # rather than assuming it exists yet. Boot scripts run in sequence: this
  # must never block indefinitely.
  DEV=""
  i=0
  while [ $i -lt 10 ]; do
    DEV=$(ls /dev/mmcblk[0-9] 2>/dev/null | head -1)
    [ -n "$DEV" ] && break
    sleep 1
    i=$((i + 1))
  done
  if [ -z "$DEV" ]; then
    echo "S19datapartition: no mmcblk device found after waiting, skipping"
    exit 0
  fi

  LOOP_DEV=$(losetup -f)
  losetup -o $((DATA_START_SECTOR * 512)) "$LOOP_DEV" "$DEV"

  mkdir -p /mnt/blackbox_data
  if ! mount "$LOOP_DEV" /mnt/blackbox_data 2>/dev/null; then
    echo "S19datapartition: formatting data region (first boot or no filesystem yet)..."
    mkfs.ext4 -F -L blackbox_data "$LOOP_DEV"
    mount "$LOOP_DEV" /mnt/blackbox_data
    dd if=/dev/zero of=/mnt/blackbox_data/.swapfile bs=1M count=512 2>/dev/null
    mkswap /mnt/blackbox_data/.swapfile >/dev/null 2>&1
  fi

  mkdir -p /mnt/blackbox_data/sessions
  # rkipc's throwaway buffer (snapshots are its only use); clear any leftovers.
  rm -rf /mnt/blackbox_data/video0 /mnt/blackbox_data/video0.prev.* /mnt/blackbox_data/video1 /mnt/blackbox_data/video2
  swapon /mnt/blackbox_data/.swapfile 2>/dev/null
  ;;
stop)
  swapoff /mnt/blackbox_data/.swapfile 2>/dev/null
  umount /mnt/blackbox_data 2>/dev/null
  ;;
*)
  exit 1
  ;;
esac
