"""Entrypoint auto-run by /etc/init.d/S99python on boot (runs /root/main.py
if present -- this is the board's existing convention).

S99python runs this in the FOREGROUND and waits for it to exit, and boot
continues only after that. S99usb0config (which assigns 172.32.0.93 to
usb0) sorts after S99python, so a server that never returns leaves the
board booted but unreachable over the network. That bricked-looking hang
happened on every boot while main.py ran the server directly. Fork, let
the parent exit immediately, and serve from the detached child.

The detached process supervises the server: the web app runs in a forked
child, and a child that dies is restarted (an unhandled crash used to
leave the board headless -- no UI, no recording -- until the next power
cycle). A child that keeps dying within seconds trips a circuit breaker
and the supervisor exits with a log line instead of looping forever in
tmpfs. killall python3 still kills everything (SIGTERM takes the
supervisor too).
"""

import os
import sys
import time

sys.path.insert(0, "/root/blackbox")

LOG_PATH = "/tmp/blackbox.log"


def _detach():
    if os.fork() > 0:
        os._exit(0)  # parent returns to S99python so boot can continue
    os.setsid()
    if os.fork() > 0:
        os._exit(0)
    devnull = os.open(os.devnull, os.O_RDONLY)
    log = os.open(LOG_PATH, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    os.dup2(devnull, 0)
    os.dup2(log, 1)
    os.dup2(log, 2)


def _run_server():
    from app import run

    run(port=5000)


if __name__ == "__main__":
    _detach()
    quick_crashes = 0
    while True:
        started = time.monotonic()
        pid = os.fork()
        if pid == 0:
            _run_server()
            os._exit(0)  # run() returning at all counts as an exit
        _, status = os.waitpid(pid, 0)
        ran_for = time.monotonic() - started
        if ran_for >= 60:
            quick_crashes = 0
        else:
            quick_crashes += 1
            if quick_crashes > 10:
                with open(LOG_PATH, "a") as f:
                    f.write(f"server crashed {quick_crashes}x in a row; giving up "
                            f"(power-cycle the board to retry)\n")
                break
        with open(LOG_PATH, "a") as f:
            f.write(f"server exited (status {status}) after {ran_for:.0f}s; restarting\n")
        time.sleep(2)
