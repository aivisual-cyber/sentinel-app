"""Arduino bridge: packet framing matches the firmware's packed struct; clean-air calibration math."""

import struct
import time

import pytest

pytest.importorskip("serial")
from hardware.arduino_bridge import ArduinoBridge  # noqa: E402


def _packet(temp=24.5, hum=40.0, smoke=12.0, co=3.0, flame=0, ir=512):
    # Layout of `struct SensorData __attribute__((packed))` in sentinel_sensors.ino
    return struct.pack('<BfffffBHB', 0xAA, temp, hum, smoke, co, 0.0, flame, ir, 0x55)


class FakeSerial:
    def __init__(self, data: bytes):
        self.buf = bytearray(data)
        self.is_open = True
        self.written = []

    @property
    def in_waiting(self):
        return len(self.buf)

    def read(self, n):
        out, self.buf = bytes(self.buf[:n]), self.buf[n:]
        return out

    def write(self, b):
        self.written.append(b)


def test_packet_size_matches_firmware_struct():
    assert ArduinoBridge.SENSOR_PACKET_SIZE == 25 == len(_packet())


def test_reads_packets_interleaved_with_text():
    stream = b"SENTINEL_READY\r\nCAL:START\r\n" + _packet(temp=21.0) + b"ACK:HEARTBEAT\r\n" + _packet(smoke=450.0, flame=1)
    bridge = ArduinoBridge(port="fake")
    bridge.serial_conn = FakeSerial(stream)
    readings = []
    bridge.callback = readings.append
    bridge.start_monitoring()
    deadline = time.time() + 2
    while len(readings) < 2 and time.time() < deadline:
        time.sleep(0.02)
    bridge.is_running = False
    assert [round(r.temperature, 1) for r in readings] == [21.0, 24.5]
    assert readings[1].smoke_ppm == pytest.approx(450.0) and readings[1].flame_detected
    assert bridge.total_readings == 2


def test_clean_air_calibration_no_longer_alarms():
    # Replicates calculatePPM_MQ2/MQ135 from the firmware with the corrected R0 = Rs / clean-air ratio
    rs_clean = 20.0
    mq2 = 613.9 * (rs_clean / (rs_clean / 9.83)) ** -2.074
    mq135 = 116.6 * (rs_clean / (rs_clean / 3.6)) ** -2.769
    assert mq2 < 300 and mq135 < 50          # below the firmware alert thresholds
    old_mq2, old_mq135 = 613.9 * 1 ** -2.074, 116.6 * 1 ** -2.769
    assert old_mq2 > 300 and old_mq135 > 50  # the old R0 = Rs calibration alarmed in clean air
