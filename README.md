# Blackbox

A dashcam-style session recorder for a Luckfox camera board (RV110x, SC3336
3MP sensor). The board serves a small web UI over its USB network link: turn
the camera on/off, start/stop a recording, browse sessions with thumbnails,
download segments or a whole session as one file. Live view is RTSP (open in
VLC). Footage is H.265 in MPEG-TS, cut into 10-second keyframe-aligned
segments that concatenate losslessly into one playable file.

The board is an embedded Linux with ~33MB usable RAM, no RTC, no ffmpeg and
a Rockchip-proprietary SD card layout. Most of this project's shape comes
from those constraints — see [Design decisions](#design-decisions-the-why).

## Repository layout

| Path | What it is |
| --- | --- |
| `device/main.py` | boot entrypoint; `/etc/init.d/S99python` runs `/root/main.py` — detaches, then supervises the server |
| `device/app.py` | web UI (stdlib `http.server`; no Flask — importing Flask alone wedged the board) |
| `device/recorder.py` | camera + session control, thumbnails, disk-space housekeeping |
| `device/rtsp_recorder.py` | pure-Python RTSP client + H.265 depacketizer + MPEG-TS muxer |
| `device/timing.py` | monotonic-anchored session clock + persistent boot counter (no RTC on board) |
| `device/sensors.py` | placeholder interface for future I2C IMU / temp-humidity sensors on `/dev/i2c-4` |
| `device/setup_data_partition.sh` | deployed as `/etc/init.d/S19datapartition`; loop-mounts the SD card's trailing ~22.9GB |
| `device/configure_rkipc.sh` | patches rkipc ini files + disables rkipc autostart; run by `deploy.sh` |
| `scripts/deploy.sh` | push everything to the board over SSH |
| `scripts/view_blackbox.sh` | PC-side: bring up the USB network, open the UI or live view in VLC |

## Board facts (load-bearing)

- Board: Luckfox (RV110x) with SC3336 3MP sensor; ~33MB usable RAM;
  **no battery-backed RTC** — wall clock resets to 1970 every boot.
- Network: USB-Ethernet gadget. Board `172.32.0.93`, host `172.32.0.1/24`,
  web UI port 5000, RTSP port 554. The gadget's MAC/interface name changes
  on every boot — `view_blackbox.sh` re-detects it.
- Storage: SD card with Rockchip's proprietary partition layout. The data
  region is the card's trailing unpartitioned space (~22.9GB of a 32GB
  card), loop-mounted at a fixed sector offset derived from *this firmware
  build's* `sd_update.txt` (`DATA_START_SECTOR` in `setup_data_partition.sh`).
  **Re-derive that offset after any reflash** — see
  [Known sharp edges](#known-sharp-edges-accepted-not-bugs).
- Python: stdlib only. There is no ffmpeg on the board.

## Design decisions (the why)

**rkipc must run for a correct picture.** The stock camera service runs the
ISP's 3A tuning (auto exposure / white balance); capturing the sensor
without it gave dark, green video. So the app starts/stops rkipc itself
(autostart is disabled in `RkLunch.sh` by `configure_rkipc.sh`) and records
from rkipc's RTSP stream rather than from the ISP directly.

**Why not rkipc's own recorder?** It closes files on a wall-clock timer but
each new file must start on a keyframe, so everything between the cut and
the next keyframe (GOP is 2s) was dropped — ~1.6s lost per 10s file,
measured. `rtsp_recorder.py` reads the RTSP stream and cuts exactly on
keyframes: no frames lost, timestamps continuous across segments, segments
concatenate into one correct file. If the connection drops mid-session it
reconnects and resumes the same timeline, advanced by the outage's
wall-clock length (the RTP clock restarts with every connection and can't
be trusted across one).

**rkipc's recorder runs anyway — as a throwaway buffer.** rkipc only saves
its periodic JPEG snapshots while its own recorder is on, so that recorder
is enabled (60s files into `video0/`) purely to produce thumbnails. The app
deletes those files every 2 seconds as they appear.

**No RTC → monotonic-anchored timing.** Every session and sensor reading is
timed against `time.monotonic()` since session start; wall clock is kept
only as a best-effort label that self-corrects if a real time source
appears. A persistent boot counter keeps session IDs sorting
chronologically across boots. Adding a DS3231 on `/dev/i2c-4` later needs
no code change here (see `timing.py`).

**Boot order matters.** `S19datapartition` (storage) → `S21appinit`
(`RkLunch.sh`: camera drivers + ini copy) → `S99python` (this app) →
`S99usb0config` (usb0 IP). `main.py` must never block: `S99python` runs it
in the foreground and boot — including the network — never finishes if it
does; that hang happened. So it detaches, and the detached process
supervises the server child (restarts it after a crash, gives up after
repeated fast crashes instead of looping forever in tmpfs).

**`RkLunch.sh` copies the factory ini over `/userdata/rkipc.ini` on every
boot**, so rkipc configuration edits go into the `/oem/usr/share/rkipc-*.ini`
copies, not `/userdata`.

## Deploying

```sh
scripts/deploy.sh                 # BOARD_IP defaults to 172.32.0.93
```

Precondition: the board is reachable over USB (`view_blackbox.sh` brings up
the host side). Deploys code to `/root/blackbox` + `/root/main.py`, installs
`S19datapartition`, configures rkipc, removes old leftovers. Then reboot the
board, or: `ssh -i ~/.ssh/luckfox_pico root@172.32.0.93 'killall python3; sleep 1; python3 /root/main.py'`.
Board-side setup (`configure_rkipc.sh`, `S19datapartition`) must be re-run
after any reflash — deploy.sh does both.

## Using

- `scripts/view_blackbox.sh` — open the web UI.
- `scripts/view_blackbox.sh live` — start the camera if needed and open
  live view in VLC.
- Web UI: Start camera (~10s) → Record → Stop. Sessions appear with size,
  length, status, and a whole-session `.ts` download. The segment currently
  being written is hidden until it closes, so downloads are always complete
  files.

Session layout on the board (`/mnt/blackbox_data/sessions/<id>/`):
`seg_NNNN.ts` (10s each), `seg_NNNN.jpg` (thumbnail), `meta.json` (status,
timing, segment count), `sensors.jsonl` (empty until real sensors are
registered), `recorder.log`. Oldest sessions are deleted automatically when
free space drops below 1.5GB (rkipc deletes its own files below
500MB–1GB, so stay well above that).

## Known sharp edges (accepted, not bugs)

- **Fixed loop offset**: if a future firmware image changes the SD layout,
  `S19datapartition`'s offset is wrong — and its fallback `mkfs.ext4` would
  format whatever now lives at that offset. Re-derive the offset from the
  new `sd_update.txt` before booting a changed image.
- **9999 segments per session** (~27.8h continuous): past that the 4-digit
  names stop matching the web app's file pattern and stop sorting correctly.
- **Camera on = continuous SD writes** (rkipc's throwaway buffer) even when
  not recording; thumbnails are only harvested during sessions.
- **No auth** on the web UI — it's for the point-to-point USB link only.
- `camera_stop()` kills every `udhcpc` on the board (rkipc's orphaned child
  holds port 554 otherwise) — don't add another udhcpc-using interface
  without revisiting this.
- Space reclamation deletes *all* other sessions before declaring the disk
  full mid-recording.
- Swap (512MB) lives on the SD card — deliberate, for the 33MB board.
- If the data mount ever disappears mid-recording, new segments land on the
  small root filesystem (no `ismount` guard in the segment path).

See `CHANGELOG.md` for what changed and when.
