# Changelog

Notable changes to the Blackbox project. Newest first. History before
2026-09-28 was not tracked in version control; a reconstructed summary is
at the bottom.

## 2026-09-28 — robustness fixes after a code review

### Fixed

- **RTSP reconnect no longer restarts the session's timeline.** A
  mid-session connection drop used to reset MPEG-TS timestamps (PTS/PCR)
  and continuity counters back to zero on the next segment, so the
  concatenated session had a backward discontinuity at every reconnect.
  The output clock and TS continuity counters now live on the
  `SegmentRecorder` instance and survive `run()` re-entry; the timeline is
  advanced by the measured wall-clock length of the outage (the RTP clock
  itself restarts with every connection, so it can't be used across one).
  Verified with a simulated drop: PTS stays monotonic across the boundary
  and the video PID's continuity counter continues (e.g. 8 → 9) instead of
  restarting at 0. (`rtsp_recorder.py`)
- **Power-loss bound is now real.** Segments were `flush()`ed every second,
  which only reaches the kernel page cache — a power cut could still lose
  several seconds of footage. They are now `fsync()`ed every second,
  matching the documented "at most the last second or so" claim.
  (`rtsp_recorder.py`)
- **Downloads no longer race the recorder.** The segment still being
  written is excluded from session listings, from the whole-session
  `all.ts` concatenation, and from direct `/file/` downloads (it keeps
  growing after its size is measured, which broke Content-Length).
  Requests that lose a race with space-reclamation deletion — a session
  vanishing mid-download or mid-page-render — now fail that one request
  instead of crashing the page. (`app.py`, `recorder.py`)
- **A failed session finalization can't leave a session stuck.** The
  post-recording bookkeeping (sensor-log stop, status update, meta.json
  write) ran outside any try/except; an error there (e.g. ENOSPC) killed
  the recording thread with the status frozen at "finishing" forever. It is
  now contained and persists an "error" status best-effort.
  (`recorder.py`)
- **Boot counter is written atomically** (temp file + rename). A torn
  direct write would reset the counter to 1 on the next boot, making new
  sessions sort before older ones from previous boots. (`timing.py`)

### Added

- **Server supervision.** The detached `main.py` process now runs the web
  app in a forked child and restarts it if it crashes — previously any
  unhandled crash left the board headless (no UI, no recording) until the
  next power cycle. A child that keeps dying within seconds trips a circuit
  breaker (after 10 quick restarts) and the supervisor exits with a log
  line instead of looping forever in tmpfs. `killall python3` still kills
  everything, as before. (`main.py`)
- **README.md** (architecture, board facts, design rationale, deploy/use
  instructions, known sharp edges) and this changelog.
- `SegmentRecorder.inflight_name` / `Recorder.inflight_segment()` — expose
  which segment file is still being written, used by the web app.

## Earlier history (reconstructed from code comments, dates approximate)

- **2026-09-04** — first version: Flask web UI, rkipc's own recorder for
  footage, `S98datapartition`, sensor placeholders.
- **Since then**: Flask dropped after `import flask` alone wedged the 33MB
  board (stdlib `http.server` since); `thumbnail.py` removed; storage init
  moved `S98` → `S19` (must mount before `S21appinit`); rkipc's own
  recorder replaced by the pure-Python RTSP recorder after measuring ~1.6s
  dropped per 10s file at keyframe boundaries; camera autostart disabled in
  favor of on-demand start from the web UI.
