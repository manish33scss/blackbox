#!/bin/sh
# Board-side setup for Blackbox, run by deploy.sh. Safe to re-run; needed
# again after any reflash, since /oem is part of the image.
#
# 1. rkipc: a JPEG snapshot every 10s (used as thumbnails) on the big store.
#    rkipc only saves snapshots while its own main-stream recorder is on, so
#    that recorder is enabled with 60s files as a throwaway buffer the
#    Blackbox app deletes as it goes -- actual footage is recorded from RTSP
#    by the app. RkLunch.sh copies the factory ini over /userdata/rkipc.ini
#    on EVERY boot, so edits go into the factory copies in /oem
#    (rkipc-300w.ini is the one used with the SC3336 sensor).
# 2. Camera off at boot: RkLunch.sh (S21appinit) normally starts rkipc; the
#    web UI starts it on demand instead. RkLunch.sh still loads the camera
#    drivers and copies the ini -- only the rkipc launch is disabled.
set -e

for INI in /oem/usr/share/rkipc-300w.ini /oem/usr/share/rkipc-400w.ini \
           /oem/usr/share/rkipc-500w.ini /userdata/rkipc.ini; do
  [ -f "$INI" ] || continue
  [ -f "$INI.orig" ] || cp "$INI" "$INI.orig"
  sed -i "s|^mount_path *=.*|mount_path                     = /mnt/blackbox_data|" "$INI"
  sed -i "/^\[storage\.0\]/,/^\[storage\.1\]/{s/^enable *=.*/enable                         = 1/;s/^file_format *=.*/file_format                    = ts/;s/^file_duration *=.*/file_duration                  = 60/}" "$INI"
  sed -i "/^\[storage\.1\]/,/^\[storage\.2\]/{s/^enable *=.*/enable                         = 0/}" "$INI"
  sed -i "/^\[storage\.2\]/,/^\[/{s/^enable *=.*/enable                         = 0/}" "$INI"
  sed -i "/^\[video\.jpeg\]/,/^\[/{s/^enable_cycle_snapshot *=.*/enable_cycle_snapshot          = 1/;s/^snapshot_interval_ms *=.*/snapshot_interval_ms           = 10000/}" "$INI"
done
echo "rkipc ini (300w):"
grep -E "^mount_path" /oem/usr/share/rkipc-300w.ini
sed -n "/^\[storage\.0\]/,/^\[storage\.2\]/p" /oem/usr/share/rkipc-300w.ini | grep -E "^\[|^enable|^file_duration"
grep -E "^(enable_cycle_snapshot|snapshot_interval_ms)" /oem/usr/share/rkipc-300w.ini

LAUNCH=/oem/usr/bin/RkLunch.sh
[ -f "$LAUNCH.orig" ] || cp "$LAUNCH" "$LAUNCH.orig"
sed -i 's|^\([[:space:]]*\)rkipc -a /oem/usr/share/iqfiles &$|\1: # blackbox: camera starts on demand from the web UI (was: rkipc -a /oem/usr/share/iqfiles \&)|' "$LAUNCH"
sed -i 's|^\([[:space:]]*\)rkipc &$|\1: # blackbox: camera starts on demand from the web UI (was: rkipc \&)|' "$LAUNCH"
echo "RkLunch.sh rkipc launch lines:"
grep -n "rkipc -a\|rkipc &" "$LAUNCH"
