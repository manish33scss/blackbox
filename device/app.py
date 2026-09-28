"""Blackbox web UI -- camera on/off, record on/off, live view links, and a
session browser.

Built on Python's stdlib http.server, NOT Flask: `import flask` alone
wedged this 33MB board. No template engine either -- HTML is built with
small string functions.

Browsers can't play RTSP, so live view is a link to rkipc's RTSP stream
(opens in VLC or similar); scripts/view_blackbox.sh live opens VLC directly.
"""

import html
import json
import re
import shutil
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from recorder import SEGMENT_SECONDS, SESSIONS_DIR, Recorder

recorder = Recorder()

SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_]+$")
FILE_RE = re.compile(r"^seg_\d{4}\.(ts|jpg)$")

PAGE_STYLE = """
body { font-family: system-ui, sans-serif; max-width: 900px; margin: 2rem auto; padding: 0 1rem; background: #111; color: #eee; }
h1 { display: flex; align-items: center; gap: 0.5rem; }
h2 { font-size: 1.1rem; margin: 1.5rem 0 0.5rem; color: #aaa; }
.dot { width: 12px; height: 12px; border-radius: 50%; display: inline-block; background: #555; }
.dot.on { background: #2a7; } .dot.starting { background: #d90; } .dot.rec { background: #e33; box-shadow: 0 0 8px #e33; }
.panel { background: #1a1a1a; border-radius: 8px; padding: 1rem; margin-top: 0.5rem; }
.row { display: flex; flex-wrap: wrap; gap: 0.75rem; align-items: center; }
table { width: 100%; border-collapse: collapse; margin-top: 0.5rem; }
th, td { text-align: left; padding: 0.5rem; border-bottom: 1px solid #333; }
a { color: #6cf; }
form { display: inline; }
button { font-size: 1rem; padding: 0.5rem 1.2rem; border-radius: 6px; border: none; cursor: pointer; color: #fff; background: #444; }
.go { background: #2a7; } .rec { background: #c33; } .off { background: #555; }
.muted { color: #888; font-size: 0.85em; }
code { background: #222; padding: 0.1rem 0.3rem; border-radius: 4px; }
.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 1rem; margin-top: 1rem; }
.card { background: #1a1a1a; border-radius: 8px; overflow: hidden; }
.card img { width: 100%; display: block; background: #000; aspect-ratio: 16/9; object-fit: cover; }
.card .nothumb { aspect-ratio: 16/9; display: flex; align-items: center; justify-content: center; color: #666; background: #000; }
.card .meta { padding: 0.5rem; font-size: 0.9em; display: flex; justify-content: space-between; align-items: center; }
dl { display: grid; grid-template-columns: auto 1fr; gap: 0.2rem 1rem; }
dt { color: #888; }
"""


def _page(title, body, refresh=None):
    meta = f'<meta http-equiv="refresh" content="{refresh}">' if refresh else ""
    return f"""<!doctype html>
<html><head><meta charset="utf-8">{meta}<title>{html.escape(title)}</title>
<style>{PAGE_STYLE}</style></head><body>{body}</body></html>"""


def _button(action, label, cls):
    return f'<form method="post" action="{action}"><button class="{cls}" type="submit">{label}</button></form>'


def _session_dir(session_id):
    if not SESSION_ID_RE.match(session_id):
        return None
    d = SESSIONS_DIR / session_id
    return d if d.is_dir() else None


def _read_meta(d):
    try:
        return json.loads((d / "meta.json").read_text())
    except (OSError, ValueError):
        return None


def _list_sessions():
    if not SESSIONS_DIR.is_dir():
        return []
    sessions = []
    for d in sorted(SESSIONS_DIR.iterdir(), reverse=True):
        meta = _read_meta(d) if d.is_dir() else None
        if meta is None:
            continue
        try:
            segs = sorted(d.glob("seg_*.ts"))
            total_bytes = sum(p.stat().st_size for p in segs)
        except OSError:
            continue  # session vanished mid-render (space reclamation)
        sessions.append({
            "id": d.name,
            "status": meta.get("status", "unknown"),
            "start_label": meta.get("start_wall_label", ""),
            "wall_clock_reliable": meta.get("wall_clock_reliable", False),
            "segments": len(segs),
            "total_mb": round(total_bytes / 1024**2, 1),
        })
    return sessions


def _duration(segments):
    secs = segments * SEGMENT_SECONDS
    return f"{secs // 60}:{secs % 60:02d}"


def render_index(host):
    st = recorder.status()
    cam = st["camera"]
    recording = st["recording"] or st["finishing"]

    dot = "rec" if st["recording"] else {"on": "on", "starting": "starting"}.get(cam, "")
    if not st["storage_mounted"]:
        camera = '<p>Storage is not mounted, so the camera can\'t be used. Check the S19datapartition boot script.</p>'
    elif cam == "off":
        camera = f'<div class="row">{_button("/camera/start", "Start camera", "go")}<span class="muted">Camera is off.</span></div>'
    elif cam == "starting":
        camera = '<p>Camera starting&hellip; (takes about 10 seconds)</p>'
    else:
        live0 = f"rtsp://{host}/live/0"
        live1 = f"rtsp://{host}/live/1"
        camera = f"""
<div class="row">{_button("/camera/stop", "Stop camera", "off")}<span class="muted">Camera is on.</span></div>
<p>Live view: <a href="{live0}">{live0}</a> (2304&times;1296) &middot; <a href="{live1}">{live1}</a> (704&times;576)</p>
<p class="muted">Browsers can't play RTSP. Open the link in VLC, or on your PC run <code>bash ~/Mee/codes/blackbox/scripts/view_blackbox.sh live</code>.</p>"""

    if st["recording"]:
        rec = (f'<div class="row">{_button("/record/stop", "&#9632; Stop recording", "rec")}'
               f'<span>Recording <strong>{html.escape(st["session_id"])}</strong> &mdash; '
               f'{st["segment_count"]} segments saved ({_duration(st["segment_count"])})</span></div>')
    elif st["finishing"]:
        rec = '<p>Stopping&hellip; saving the last segment.</p>'
    elif cam == "on":
        rec = f'<div class="row">{_button("/record/start", "&#9679; Record", "rec")}<span class="muted">Not recording.</span></div>'
    else:
        rec = '<p class="muted">Start the camera first.</p>'

    free = f'{st["free_gb"]} GB free' if st["free_gb"] is not None else "not mounted"

    rows = ""
    for s in _list_sessions():
        clock_note = "" if s["wall_clock_reliable"] else ' <span class="muted">(clock unsynced)</span>'
        rows += (
            f'<tr><td><a href="/session/{s["id"]}">{s["id"]}</a></td>'
            f'<td>{html.escape(s["start_label"])}{clock_note}</td>'
            f'<td>{_duration(s["segments"])}</td><td>{s["total_mb"]} MB</td>'
            f'<td>{html.escape(s["status"])}</td>'
            f'<td><a href="/session/{s["id"]}/all.ts">download</a></td></tr>'
        )
    if not rows:
        rows = '<tr><td colspan="6" class="muted">No sessions yet.</td></tr>'

    body = f"""
<h1><span class="dot {dot}"></span>Blackbox</h1>
<h2>Camera</h2><div class="panel">{camera}</div>
<h2>Recording</h2><div class="panel">{rec}</div>
<h2>Sessions <span class="muted">({free})</span></h2>
<table>
<tr><th>Session</th><th>Started</th><th>Length</th><th>Size</th><th>Status</th><th></th></tr>
{rows}
</table>
"""
    # Keep the page current while something is changing.
    refresh = 3 if cam == "starting" or recording else None
    return _page("Blackbox", body, refresh)


def render_session(session_id, meta, segs):
    clock_note = "" if meta.get("wall_clock_reliable") else ' <span class="muted">(clock unsynced)</span>'
    d = SESSIONS_DIR / session_id
    cards = ""
    for i, ts in enumerate(segs):
        jpg = ts.replace(".ts", ".jpg")
        start = _duration(i)
        thumb = (f'<img src="/file/{session_id}/{jpg}" loading="lazy">' if (d / jpg).exists()
                 else '<div class="nothumb">no preview</div>')
        cards += f"""
<div class="card">
  <a href="/file/{session_id}/{ts}">{thumb}</a>
  <div class="meta"><span>{ts} <span class="muted">@ {start}</span></span><a href="/file/{session_id}/{ts}">download</a></div>
</div>"""
    if not cards:
        cards = '<p class="muted">No segments saved.</p>'

    body = f"""
<p><a href="/">&larr; back</a></p>
<h1>{session_id}</h1>
<dl>
  <dt>Length</dt><dd>{_duration(len(segs))} ({len(segs)} &times; {SEGMENT_SECONDS}s segments)</dd>
  <dt>Video</dt><dd>{html.escape(meta.get("video", ""))}, MPEG-TS</dd>
  <dt>Status</dt><dd>{html.escape(meta.get("status", ""))}</dd>
  <dt>Started</dt><dd>{html.escape(meta.get("start_wall_label", ""))}{clock_note}</dd>
</dl>
<p><a href="/session/{session_id}/all.ts">Download whole session as one .ts</a></p>
<div class="grid">{cards}</div>
"""
    return _page(f"Blackbox - {session_id}", body)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # default logging is noisy and we're I/O constrained

    def _send_html(self, body, code=200):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_redirect(self, location):
        self.send_response(303)
        self.send_header("Location", location)
        self.end_headers()

    def _send_404(self):
        self._send_html("<h1>404</h1>", code=404)

    def _send_files(self, paths, mimetype, filename=None):
        try:
            size = sum(p.stat().st_size for p in paths)
        except OSError:
            return self._send_404()  # files vanished mid-request (space reclamation)
        self.send_response(200)
        self.send_header("Content-Type", mimetype)
        self.send_header("Content-Length", str(size))
        if filename:
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.end_headers()
        try:
            for p in paths:
                with open(p, "rb") as f:
                    shutil.copyfileobj(f, self.wfile)
        except OSError:
            pass  # a file vanished or the client hung up; the transfer just ends short

    def do_GET(self):
        parts = [p for p in urlparse(self.path).path.split("/") if p]

        if not parts:
            host = (self.headers.get("Host") or "172.32.0.93").split(":")[0]
            return self._send_html(render_index(host))

        if parts[0] == "session" and len(parts) in (2, 3):
            d = _session_dir(parts[1])
            meta = _read_meta(d) if d else None
            if meta is None:
                return self._send_404()
            segs = sorted(p.name for p in d.glob("seg_*.ts"))
            inflight = recorder.inflight_segment(parts[1])
            if inflight in segs:
                segs.remove(inflight)  # still being written; it grows under our feet
            if len(parts) == 2:
                return self._send_html(render_session(parts[1], meta, segs))
            if parts[2] == "all.ts" and segs:
                # MPEG-TS segments play back correctly when simply concatenated.
                return self._send_files([d / s for s in segs], "video/mp2t", f"{parts[1]}.ts")
            return self._send_404()

        if parts[0] == "file" and len(parts) == 3:
            d = _session_dir(parts[1])
            if d is None or not FILE_RE.match(parts[2]) or not (d / parts[2]).is_file():
                return self._send_404()
            if parts[2] == recorder.inflight_segment(parts[1]):
                return self._send_404()  # segment still being written
            if parts[2].endswith(".jpg"):
                return self._send_files([d / parts[2]], "image/jpeg")
            return self._send_files([d / parts[2]], "video/mp2t", f"{parts[1]}_{parts[2]}")

        self._send_404()

    def do_POST(self):
        actions = {
            "/camera/start": recorder.camera_start,
            "/camera/stop": recorder.camera_stop,
            "/record/start": recorder.start_session,
            "/record/stop": recorder.stop_session,
        }
        action = actions.get(urlparse(self.path).path)
        if action is None:
            return self._send_404()
        action()
        self._send_redirect("/")


def run(port=5000):
    recorder.start_background()
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    server.serve_forever()


if __name__ == "__main__":
    run()
