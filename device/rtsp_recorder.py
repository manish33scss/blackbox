"""Record rkipc's live RTSP stream (H.265) into 10-second MPEG-TS segments.

Why not rkipc's own recorder: it closes files on a wall-clock timer but each
new file has to start on a keyframe, so everything between the cut and the
next keyframe (GOP is 2s) was dropped -- ~1.6s lost per 10s file, measured.
Reading the RTSP stream ourselves lets us cut exactly on keyframes, so no
frames are lost, and timestamps run continuously across segments, so the
segments concatenate into one correct file.

If the connection drops mid-session the recorder reconnects and resumes the
same output timeline: frame times advance by the measured wall-clock length
of the outage and the TS continuity counters keep counting, so the
concatenated session stays monotonic (a backward timestamp jump is what
breaks players).

No ffmpeg on the board, so this is a minimal RTSP client (RTP over the RTSP
TCP connection), H.265 depacketizer (RFC 7798: single NALs + fragmentation
units, which is all rkipc sends) and MPEG-TS writer, in plain Python.
"""

import base64
import os
import re
import socket
import struct
import time

TS_CLOCK = 90000  # RTP H.265 and MPEG-TS PTS both use a 90kHz clock
VIDEO_PID = 0x100
PMT_PID = 0x1000
STREAM_TYPE_HEVC = 0x24
PTS_DELAY = 9000  # PTS runs 100ms ahead of PCR, as decoders expect
AUD = b"\x00\x00\x00\x01\x46\x01\x50"  # H.265 access unit delimiter (TS wants one per frame)
START_CODE = b"\x00\x00\x00\x01"


# --- MPEG-TS ------------------------------------------------------------------

def _crc32_mpeg(data):
    crc = 0xFFFFFFFF
    for b in data:
        crc ^= b << 24
        for _ in range(8):
            crc = ((crc << 1) ^ 0x04C11DB7) & 0xFFFFFFFF if crc & 0x80000000 else (crc << 1) & 0xFFFFFFFF
    return crc


def _section(table_id, body):
    table = struct.pack(">BH", table_id, 0xB000 | (len(body) + 4)) + body
    return table + struct.pack(">I", _crc32_mpeg(table))


PAT_SECTION = _section(0x00, struct.pack(">HBBBHH", 1, 0xC1, 0, 0, 1, 0xE000 | PMT_PID))
PMT_SECTION = _section(0x02, struct.pack(">HBBBHHBHH", 1, 0xC1, 0, 0,
                                         0xE000 | VIDEO_PID, 0xF000,  # PCR PID; no program info
                                         STREAM_TYPE_HEVC, 0xE000 | VIDEO_PID, 0xF000))


def _pts_bytes(pts):
    pts &= (1 << 33) - 1
    return bytes([
        0x21 | ((pts >> 29) & 0x0E),
        (pts >> 22) & 0xFF, 0x01 | ((pts >> 14) & 0xFE),
        (pts >> 7) & 0xFF, 0x01 | ((pts << 1) & 0xFE),
    ])


class TsWriter:
    """Writes one H.265 stream as MPEG-TS. Continuity counters live in `cc`
    (pid -> counter); one shared dict is handed to every segment's writer,
    so counters never restart and segments concatenate into a stream a
    player sees as unbroken."""

    def __init__(self, f, cc=None):
        self.f = f
        self.cc = cc if cc is not None else {}
        for pid, section in ((0, PAT_SECTION), (PMT_PID, PMT_SECTION)):
            payload = b"\x00" + section  # pointer_field, then the table
            self.f.write(self._header(pid, True, False) + payload + b"\xff" * (184 - len(payload)))

    def _header(self, pid, pusi, has_af):
        cc = self.cc.get(pid, 0)
        self.cc[pid] = (cc + 1) & 0x0F
        return struct.pack(">BHB", 0x47, (0x4000 if pusi else 0) | pid, (0x30 if has_af else 0x10) | cc)

    def write_frame(self, t, annexb, key):
        """t: 90kHz time of this frame, sent as PCR; PTS = t + PTS_DELAY."""
        pes = b"\x00\x00\x01\xe0\x00\x00\x80\x80\x05" + _pts_bytes(t + PTS_DELAY) + annexb
        out = bytearray()
        pos = 0
        while pos < len(pes):
            first = pos == 0
            af = None
            if first:  # PCR (+ random-access flag on keyframes) rides on the frame's first packet
                pcr = t & ((1 << 33) - 1)
                af = bytes([0x50 if key else 0x10]) + struct.pack(">IH", pcr >> 1, ((pcr & 1) << 15) | 0x7E00)
            room = 184 - (1 + len(af) if af is not None else 0)
            rem = len(pes) - pos
            if rem < room:  # last packet: fill the gap with adaptation-field stuffing
                af_len = 183 - rem
                if af is None:
                    af = b"" if af_len == 0 else b"\x00"
                af += b"\xff" * (af_len - len(af))
                room = rem
            if af is not None:
                pkt = self._header(VIDEO_PID, first, True) + bytes([len(af)]) + af + pes[pos:pos + room]
            else:
                pkt = self._header(VIDEO_PID, first, False) + pes[pos:pos + room]
            out += pkt
            pos += room
        self.f.write(out)


# --- RTSP / RTP -----------------------------------------------------------------

class RtspH265:
    """Minimal RTSP client for rkipc's rtsp_demo server: video track only,
    RTP interleaved over the RTSP TCP connection."""

    def __init__(self, url, timeout=5):
        m = re.match(r"rtsp://([^/:]+)(?::(\d+))?", url)
        self.url = url
        self.sock = socket.create_connection((m.group(1), int(m.group(2) or 554)), timeout=timeout)
        self.buf = b""
        self.cseq = 0
        self.param_sets = []
        _, sdp = self._request("DESCRIBE", url, "Accept: application/sdp\r\n")
        video = sdp.split("m=video", 1)[1].split("m=", 1)[0]
        for key in ("sprop-vps", "sprop-sps", "sprop-pps"):
            mm = re.search(key + r"=([A-Za-z0-9+/=]+)", video)
            if mm:
                self.param_sets.append(base64.b64decode(mm.group(1)))
        control = re.search(r"a=control:(\S+)", video).group(1)
        track = control if control.startswith("rtsp://") else url.rstrip("/") + "/" + control
        head, _ = self._request("SETUP", track, "Transport: RTP/AVP/TCP;unicast;interleaved=0-1\r\n")
        self.session = re.search(r"Session: *([^;\r\n]+)", head).group(1)
        self._request("PLAY", url, f"Session: {self.session}\r\nRange: npt=0.000-\r\n")
        self.last_keepalive = time.monotonic()

    def _send(self, method, url, extra=""):
        self.cseq += 1
        self.sock.sendall(f"{method} {url} RTSP/1.0\r\nCSeq: {self.cseq}\r\n{extra}\r\n".encode())

    def _read_response(self, check=True):
        while b"\r\n\r\n" not in self.buf:
            self._fill()
        head, self.buf = self.buf.split(b"\r\n\r\n", 1)
        m = re.search(rb"Content-Length: *(\d+)", head, re.I)
        n = int(m.group(1)) if m else 0
        while len(self.buf) < n:
            self._fill()
        body, self.buf = self.buf[:n], self.buf[n:]
        head = head.decode(errors="replace")
        if check and " 200 " not in head.split("\r\n")[0]:
            raise IOError(f"RTSP error: {head.splitlines()[0]}")
        return head, body.decode(errors="replace")

    def _request(self, method, url, extra=""):
        self._send(method, url, extra)
        return self._read_response()

    def _fill(self):
        d = self.sock.recv(65536)
        if not d:
            raise IOError("RTSP connection closed")
        self.buf += d

    def close(self):
        try:
            self._send("TEARDOWN", self.url, f"Session: {self.session}\r\n")
        except OSError:
            pass
        self.sock.close()

    def frames(self):
        """Yield (rtp_timestamp, [NAL units], is_keyframe) per video frame.

        rkipc sends VPS/SPS/PPS as their own RTP "frame" (marker set) right
        before each keyframe; those are held and attached to the next picture
        so every yielded frame contains a picture."""
        nals, pending, frag, ts = [], [], None, None
        while True:
            if time.monotonic() - self.last_keepalive > 30:
                # rtsp_demo answers GET_PARAMETER with 501, so keep alive with OPTIONS
                self._send("OPTIONS", self.url, f"Session: {self.session}\r\n")
                self.last_keepalive = time.monotonic()
            while len(self.buf) < 4:
                self._fill()
            if self.buf[0] != 0x24:  # an RTSP reply (keepalive) interleaved with data
                self._read_response(check=False)
                continue
            channel, length = self.buf[1], struct.unpack(">H", self.buf[2:4])[0]
            while len(self.buf) < 4 + length:
                self._fill()
            pkt, self.buf = self.buf[4:4 + length], self.buf[4 + length:]
            if channel != 0 or len(pkt) < 13:
                continue
            hl = 12 + 4 * (pkt[0] & 0x0F)
            if pkt[0] & 0x10:  # header extension
                hl += 4 + 4 * struct.unpack(">H", pkt[hl + 2:hl + 4])[0]
            end = len(pkt) - (pkt[-1] if pkt[0] & 0x20 else 0)  # padding
            marker = pkt[1] & 0x80
            pts = struct.unpack(">I", pkt[4:8])[0]
            if ts is not None and pts != ts and nals:  # lost the marker packet; flush
                if _has_picture(nals):
                    yield ts, pending + nals, _is_key(nals)
                    pending = []
                else:
                    pending += nals
                nals = []
            ts = pts
            payload = pkt[hl:end]
            ntype = (payload[0] >> 1) & 0x3F
            if ntype == 49:  # fragmentation unit
                fu = payload[2]
                if fu & 0x80:
                    frag = bytearray(bytes([(payload[0] & 0x81) | ((fu & 0x3F) << 1), payload[1]]))
                if frag is not None:
                    frag += payload[3:]
                if fu & 0x40 and frag is not None:
                    nals.append(bytes(frag))
                    frag = None
            elif ntype == 48:  # aggregation packet
                i = 2
                while i + 2 <= len(payload):
                    n = struct.unpack(">H", payload[i:i + 2])[0]
                    nals.append(payload[i + 2:i + 2 + n])
                    i += 2 + n
            else:
                nals.append(payload)
            if marker and nals:
                if _has_picture(nals):
                    yield ts, pending + nals, _is_key(nals)
                    pending = []
                else:
                    pending += nals
                nals = []


def _is_key(nals):
    return any(16 <= ((n[0] >> 1) & 0x3F) <= 21 for n in nals)


def _has_picture(nals):
    return any(((n[0] >> 1) & 0x3F) < 32 for n in nals)  # VCL NAL types are 0-31


# --- segmenting recorder ---------------------------------------------------------

class SegmentRecorder:
    """Writes seg_0000.ts, seg_0001.ts, ... into out_dir. Starts on the first
    keyframe, cuts on the first keyframe at least `segment_s` after the
    current segment began. Timestamps continue across segments -- and across
    reconnects: run() may be called again after a connection error (see
    Recorder._record) and the timeline picks up where it left off."""

    def __init__(self, url, out_dir, segment_s=10, on_segment=None):
        self.url = url
        self.out_dir = out_dir
        self.segment_ticks = segment_s * TS_CLOCK
        self.on_segment = on_segment  # called with (index) whenever a segment is closed
        self.index = -1
        self.frames_written = 0
        self._stop = False
        self._f = None
        self._t = 0  # unwrapped 90kHz output time; survives reconnects
        self._cc = {}  # TS continuity counters, shared by every segment
        self._last_frame_mono = None  # wall time of the last frame, to bridge outages

    @property
    def inflight_name(self):
        """Filename of the segment currently being written (still growing),
        or None once it's closed. The web app hides this file from listings
        and downloads until then, because it keeps growing after any stat()
        of it."""
        return f"seg_{self.index:04d}.ts" if self._f is not None else None

    def stop(self):
        self._stop = True

    def run(self):
        """Record until stopped or the connection drops. Callable again after
        an exception: the output timeline and continuity counters continue
        from where they stopped, advanced by the wall-clock length of the
        outage (the RTP clock itself restarts with every connection, so it
        can't be trusted across one)."""
        src = RtspH265(self.url)
        try:
            origin = seg_start = prev_rtp = None
            writer = None
            last_sync = time.monotonic()
            for rtp_ts, nals, key in src.frames():
                if self._stop:
                    break
                if origin is None:
                    if not key:
                        continue  # wait for a keyframe so the file starts decodable
                    origin = prev_rtp = rtp_ts
                    if self._last_frame_mono is not None:
                        # reconnect: bridge the timeline across the outage so
                        # timestamps never jump backwards
                        self._t += int((time.monotonic() - self._last_frame_mono) * TS_CLOCK)
                self._t += (rtp_ts - prev_rtp) & 0xFFFFFFFF
                prev_rtp = rtp_ts
                t = self._t
                if key and (writer is None or t - seg_start >= self.segment_ticks - TS_CLOCK // 50):
                    self._close()
                    self.index += 1
                    self._f = open(f"{self.out_dir}/seg_{self.index:04d}.ts", "wb")
                    writer = TsWriter(self._f, cc=self._cc)
                    seg_start = t
                    if not any(((n[0] >> 1) & 0x3F) == 32 for n in nals):
                        nals = src.param_sets + nals  # make each segment self-contained
                writer.write_frame(t + TS_CLOCK, AUD + b"".join(START_CODE + n for n in nals), key)
                self.frames_written += 1
                self._last_frame_mono = time.monotonic()
                if time.monotonic() - last_sync > 1:  # bound what a power loss can take
                    self._f.flush()
                    os.fsync(self._f.fileno())  # flush alone only reaches the page cache
                    last_sync = time.monotonic()
        finally:
            self._close()
            src.close()

    def _close(self):
        if self._f:
            self._f.flush()
            try:
                os.fsync(self._f.fileno())
            except OSError:
                pass
            self._f.close()
            self._f = None
            if self.on_segment:
                self.on_segment(self.index)
