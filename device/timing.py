"""Monotonic-anchored session clock.

The board has no battery-backed RTC (confirmed: no /dev/rtc*, date resets to
the 1970 epoch on every boot). time.time() is therefore NOT trustworthy as
an absolute timestamp today -- only time.monotonic() is reliable within a
boot session. Every session and sensor reading is timed against monotonic
seconds since session start; wall-clock is kept only as a best-effort human
label that happens to self-correct for free the moment a real time source
exists.

Future RTC hook: a DS3231-class chip on /dev/i2c-4 would set the system
clock (e.g. via `hwclock -s` after reading the chip) once, at boot, before
any SessionClock is created. Nothing in this module or its callers would
need to change -- time.time() would just start returning real values.
"""

import time


def next_boot_number(state_path):
    """Persistent counter, bumped once per process (i.e. once per boot, since
    main.py starts the app once at boot). Needed because wall-clock restarts
    at 1970 every boot, so without it a new session can sort before an older
    session from a previous boot."""
    try:
        n = int(state_path.read_text().strip()) + 1
    except (OSError, ValueError):
        n = 1
    tmp = state_path.parent / (state_path.name + ".tmp")
    tmp.write_text(f"{n}\n")
    tmp.replace(state_path)  # atomic: a power cut can't leave the counter truncated
    return n


class SessionClock:
    def __init__(self, boot):
        self.boot = boot
        self.start_monotonic = time.monotonic()
        self.start_wall = time.time()

    def offset(self):
        """Seconds elapsed since session start (authoritative for ordering/sync)."""
        return time.monotonic() - self.start_monotonic

    def session_id(self):
        """Directory name that sorts chronologically without a real clock:
        boot counter, then zero-padded ms since boot, then the (currently
        meaningless, self-correcting once an RTC exists) wall-clock label."""
        try:
            label = time.strftime("%Y%m%d_%H%M%S", time.localtime(self.start_wall))
        except (OverflowError, OSError):
            label = "unknown"
        return f"b{self.boot:05d}_t{int(self.start_monotonic * 1000):010d}_{label}"

    def to_dict(self):
        return {
            "boot": self.boot,
            "start_monotonic": self.start_monotonic,
            "start_wall": self.start_wall,
            "start_wall_label": time.strftime(
                "%Y-%m-%d %H:%M:%S", time.localtime(self.start_wall)
            ),
            "wall_clock_reliable": self.start_wall > 1_704_067_200,  # sanity: past 2024-01-01
        }
