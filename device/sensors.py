"""Sensor placeholder interface.

No sensor hardware is connected yet. This defines the plug-in shape so that
wiring in real IMU / temperature-humidity sensors later is a drop-in (new
SensorReader subclass + register it in main.py's sensor list) rather than a
rewrite of the recording/logging path. Both are natural I2C devices and
would share the already-confirmed-present /dev/i2c-4 bus.
"""

import json
import threading
import time
from abc import ABC, abstractmethod


class SensorReader(ABC):
    name = "unnamed"

    @abstractmethod
    def read(self):
        """Return a dict of readings, or None if the sensor isn't connected/available.
        Must not raise -- a failed read is a None, not an exception, so the
        polling loop never dies because one sensor misbehaves."""
        raise NotImplementedError


class ImuSensor(SensorReader):
    """Placeholder. Real implementation: read accel/gyro from an I2C IMU
    (e.g. MPU6050-class) on /dev/i2c-4 once hardware exists."""

    name = "imu"

    def read(self):
        return None


class TempHumiditySensor(SensorReader):
    """Placeholder. Real implementation: read an I2C temp/humidity sensor
    (e.g. SHT31-class) on /dev/i2c-4 once hardware exists."""

    name = "temp_humidity"

    def read(self):
        return None


class SensorLog:
    """Polls a list of SensorReaders on a background thread and appends
    tick-stamped JSON lines to sensors.jsonl in the session directory.
    A reading of None (sensor not connected) is silently skipped, not logged --
    the file simply stays empty/short until real sensors are registered."""

    def __init__(self, session_dir, clock, sensors, interval_s=1.0):
        self.path = session_dir / "sensors.jsonl"
        self.clock = clock
        self.sensors = sensors
        self.interval_s = interval_s
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        if not self.sensors:
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=self.interval_s * 2)

    def _run(self):
        with open(self.path, "a") as f:
            while not self._stop.is_set():
                tick = self.clock.offset()
                for sensor in self.sensors:
                    try:
                        value = sensor.read()
                    except Exception:
                        value = None
                    if value is not None:
                        f.write(json.dumps({"tick": tick, "sensor": sensor.name, **value}) + "\n")
                        f.flush()
                self._stop.wait(self.interval_s)
