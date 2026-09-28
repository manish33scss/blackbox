"""Camera + session control.

Camera: rkipc, the stock camera service. It must be running for a correct
picture -- it runs the ISP's 3A tuning (auto exposure / white balance);
capturing without it gave dark, green video -- and it serves live view over
RTSP. deploy.sh stops RkLunch.sh from starting it at boot, so the camera is
off until camera_start().

Recording: rtsp_recorder.SegmentRecorder reads rkipc's RTSP stream and
writes the session as 10-second .ts segments, cut exactly on keyframes.
(rkipc's own recorder drops ~1.6s at every file boundary, so it isn't used
for footage.)

Thumbnails: rkipc only saves its periodic JPEG snapshots while its own main
recorder is on, so that recorder runs as a throwaway buffer in BUFFER_DIR
(deleted as it's written). While a session records, each new snapshot is
moved in as the thumbnail of the segment being written.

Crash safety: segments are flushed and fsynced every second, so a power loss
costs at most the last second or so (a .ts file cut short still plays).
Sessions still marked "recording" at startup are marked "interrupted".
"""

import json
import os
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path

from rtsp_recorder import SegmentRecorder
from sensors import SensorLog
from timing import SessionClock, next_boot_number

DATA_MOUNT = Path("/mnt/blackbox_data")
BUFFER_DIR = DATA_MOUNT / "video0"  # rkipc [storage] mount_path + [storage.0] folder_name
SESSIONS_DIR = DATA_MOUNT / "sessions"
RTSP_URL = "rtsp://127.0.0.1/live/0"
SEGMENT_SECONDS = 10
POLL_S = 2
# rkipc auto-deletes its own files below 500-1000MB free; stay well above that.
MIN_FREE_BYTES = 1500 * 1024 * 1024

RKIPC = "/oem/usr/bin/rkipc"
IQ_DIR = "/oem/usr/share/iqfiles"
RK_HOME = "/oem"
RTSP_PORT_HEX = "022A"  # 554


def _free_bytes(path):
    st = os.statvfs(path)
    return st.f_bavail * st.f_frsize


def make_room(base_dir, keep, min_free=MIN_FREE_BYTES):
    """Delete whole sessions, oldest first, until `min_free` bytes are free.
    Never touches `keep` (the session being recorded). Session dir names sort
    chronologically (see SessionClock.session_id), so oldest is first by name.
    Returns (enough_space, deleted_names)."""
    base = Path(base_dir)
    deleted = []
    if _free_bytes(base) >= min_free:
        return True, deleted
    for d in sorted(p for p in base.iterdir() if p.is_dir() and p != Path(keep)):
        shutil.rmtree(d, ignore_errors=True)
        deleted.append(d.name)
        os.sync()  # make sure statvfs sees the freed blocks
        if _free_bytes(base) >= min_free:
            return True, deleted
    return _free_bytes(base) >= min_free, deleted


def storage_mounted():
    return os.path.ismount(DATA_MOUNT)


def _pids(comm):
    pids = []
    for p in Path("/proc").iterdir():
        if p.name.isdigit():
            try:
                if (p / "comm").read_text().strip() == comm:
                    pids.append(int(p.name))
            except OSError:
                pass
    return pids


def rtsp_ready():
    """rkipc's RTSP server is listening, i.e. live view is up."""
    try:
        lines = Path("/proc/net/tcp").read_text().splitlines()[1:]
    except OSError:
        return False
    return any(f.split()[1].endswith(":" + RTSP_PORT_HEX) and f.split()[3] == "0A" for f in lines)


def _rk_env():
    """What /etc/profile.d/RkEnv.sh sets up for rkipc (its libs live in /oem)."""
    env = os.environ.copy()
    env["HOME"] = RK_HOME
    env["PATH"] = f"{env.get('PATH', '')}:{RK_HOME}:{RK_HOME}/bin:{RK_HOME}/usr/bin:{RK_HOME}/sbin:{RK_HOME}/usr/sbin"
    env["LD_LIBRARY_PATH"] = f"{RK_HOME}/usr/lib:{RK_HOME}/lib:{env.get('LD_LIBRARY_PATH', '')}"
    return env


def _read_meta(d):
    try:
        return json.loads((d / "meta.json").read_text())
    except (OSError, ValueError):
        return None


def _write_meta(d, meta):
    tmp = d / "meta.json.tmp"
    tmp.write_text(json.dumps(meta, indent=2))
    tmp.replace(d / "meta.json")


class Session:
    def __init__(self, sessions_dir, clock, sensors):
        self.clock = clock
        self.id = clock.session_id()
        self.dir = sessions_dir / self.id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.status = "recording"
        self.rec = SegmentRecorder(RTSP_URL, str(self.dir), SEGMENT_SECONDS,
                                   on_segment=lambda i: self.write_meta())
        self.sensor_log = SensorLog(self.dir, clock, sensors)
        self.sensor_log.start()
        self.write_meta()

    def segments(self):
        return len(list(self.dir.glob("seg_*.ts")))

    def write_meta(self):
        _write_meta(self.dir, {
            "status": self.status,
            "container": "ts",
            "video": "hevc 2304x1296 @25fps",
            "segment_seconds": SEGMENT_SECONDS,
            "segment_count": self.segments(),
            **self.clock.to_dict(),
        })

    def log(self, msg):
        with open(self.dir / "recorder.log", "a") as f:
            f.write(msg + "\n")


class Recorder:
    """Camera on/off plus one recording session at a time."""

    def __init__(self, sensors=None, sessions_dir=SESSIONS_DIR, buffer_dir=BUFFER_DIR):
        self.sensors = sensors or []
        self.sessions_dir = Path(sessions_dir)
        self.buffer_dir = Path(buffer_dir)
        self._session = None
        self._session_thread = None
        self._boot = None
        self._lock = threading.Lock()
        self._thread = None
        self._proc = None  # rkipc, when we started it (so we can reap it)

    # --- lifecycle -------------------------------------------------------

    def start_background(self):
        if self._thread:
            return
        if storage_mounted():
            self.sessions_dir.mkdir(parents=True, exist_ok=True)
            for d in self.sessions_dir.iterdir():
                meta = _read_meta(d) if d.is_dir() else None
                if meta and meta.get("status") in ("recording", "finishing"):
                    meta["status"] = "interrupted"  # power was lost mid-recording
                    meta["segment_count"] = len(list(d.glob("seg_*.ts")))
                    _write_meta(d, meta)
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self):
        while True:
            try:
                self.tick()
            except Exception as e:  # never let the housekeeping die
                try:
                    with open("/tmp/blackbox.log", "a") as f:
                        f.write(f"tick error: {e!r}\n")
                except OSError:
                    pass
            time.sleep(POLL_S)

    # --- camera ----------------------------------------------------------

    def camera_state(self):
        if self._proc is not None:
            self._proc.poll()  # reap it if it died, so it doesn't linger as a zombie
        if not _pids("rkipc"):
            return "off"
        return "on" if rtsp_ready() else "starting"

    def camera_start(self):
        with self._lock:
            if self.camera_state() != "off" or not storage_mounted():
                return
            self._proc = subprocess.Popen(
                [RKIPC, "-a", IQ_DIR], cwd="/", env=_rk_env(),
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, start_new_session=True,
            )

    def camera_stop(self):
        self.stop_session(wait=True)
        for pid in _pids("rkipc"):
            os.kill(pid, signal.SIGTERM)
        deadline = time.monotonic() + 8
        while _pids("rkipc") and time.monotonic() < deadline:
            if self._proc is not None:
                self._proc.poll()
            time.sleep(0.3)
        for pid in _pids("rkipc"):
            os.kill(pid, signal.SIGKILL)
        if self._proc is not None:
            try:
                self._proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                pass
            self._proc = None
        # rkipc's udhcpc child inherits its RTSP socket and keeps port 554
        # bound after rkipc exits, so the next start can't serve live view.
        # The vendor's RkLunch-stop.sh kills it for the same reason.
        for pid in _pids("udhcpc"):
            os.kill(pid, signal.SIGTERM)
        self.tick()

    # --- recording -------------------------------------------------------

    def toggle_recording(self):
        """For the physical button: Record if idle (starting the camera if
        needed), otherwise Stop."""
        if self._session and self._session.status == "recording":
            self.stop_session()
        else:
            self.start_session()

    def start_session(self):
        if self.camera_state() == "off":
            self.camera_start()
        with self._lock:
            if self._session and self._session.status in ("recording", "finishing"):
                return self._session
            if not storage_mounted():
                return None
            self.sessions_dir.mkdir(parents=True, exist_ok=True)
            if self._boot is None:
                # Lazily, so merely importing the app (deploy's check) doesn't bump it.
                self._boot = next_boot_number(self.sessions_dir / ".boot_counter")
            s = Session(self.sessions_dir, SessionClock(self._boot), self.sensors)
            self._session = s
            self._session_thread = threading.Thread(target=self._record, args=(s,), daemon=True)
            self._session_thread.start()
            return s

    def _record(self, s):
        """Runs the segment recorder, retrying the RTSP connection while the
        camera is still starting up."""
        final = "stopped"
        for attempt in range(30):
            try:
                s.rec.run()
                break
            except Exception as e:
                if s.rec._stop:
                    break
                if self.camera_state() == "off":
                    s.log(f"camera went off: {e!r}")
                    final = "error"
                    break
                if attempt == 29:
                    s.log(f"gave up connecting to live stream: {e!r}")
                    final = "error"
                    break
                if s.rec.frames_written:
                    s.log(f"stream dropped, reconnecting: {e!r}")
                time.sleep(1)
        try:
            s.sensor_log.stop()
            s.status = final if s.status != "disk_full" else "disk_full"
            s.write_meta()
        except Exception as e:
            # A failed finalization (e.g. ENOSPC writing meta.json) must not
            # kill this thread with the status frozen at "finishing"; persist
            # an error state best-effort instead.
            s.status = "error"
            try:
                s.log(f"finalization failed: {e!r}")
                s.write_meta()
            except Exception:
                pass

    def stop_session(self, wait=False):
        s, t = self._session, self._session_thread
        if not s or s.status != "recording":
            return
        s.status = "finishing"
        s.write_meta()
        s.rec.stop()
        if wait and t:
            t.join(timeout=15)

    def status(self):
        s = self._session
        st = {
            "camera": self.camera_state(),
            "recording": bool(s and s.status == "recording"),
            "finishing": bool(s and s.status == "finishing"),
            "session_id": s.id if s else None,
            "segment_count": (s.rec.index + 1) if s else 0,
            "storage_mounted": storage_mounted(),
            "free_gb": None,
        }
        if st["storage_mounted"]:
            st["free_gb"] = round(_free_bytes(DATA_MOUNT) / 1024**3, 1)
        return st

    def inflight_segment(self, session_id):
        """Filename of the segment still being written in that session, or
        None. That file keeps growing after any stat() of it, so the web app
        hides it from listings and downloads until it's closed."""
        s = self._session
        if s and s.id == session_id:
            return s.rec.inflight_name
        return None

    # --- housekeeping ----------------------------------------------------

    def tick(self):
        """Empty rkipc's throwaway buffer, hand snapshots to the recording
        session as thumbnails, and keep free space above MIN_FREE_BYTES."""
        with self._lock:
            if not storage_mounted() or not self.buffer_dir.is_dir():
                return
            s = self._session
            recording = s is not None and s.status == "recording"
            for p in sorted(self.buffer_dir.iterdir()):
                if p.suffix == ".ts":
                    # rkipc may still be writing it; the space comes back when it closes it
                    p.unlink()
                elif p.suffix == ".jpeg":
                    thumb = s.dir / f"seg_{max(s.rec.index, 0):04d}.jpg" if recording else None
                    if thumb is not None and not thumb.exists():
                        shutil.move(str(p), thumb)
                    else:
                        p.unlink()
            if recording:
                enough, deleted = make_room(self.sessions_dir, keep=s.dir)
                if deleted:
                    s.log(f"low space: deleted oldest sessions {deleted}")
                if not enough:
                    s.log("disk full: only this session left, stopping")
                    s.status = "disk_full"
                    s.rec.stop()
