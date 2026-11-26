import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

WORKER = Path(__file__).resolve().parents[1] / "agent" / "manual_drive.py"


@pytest.fixture
def fake_driver(tmp_path):
    source = tmp_path / "robot_navigation.py"
    source.write_text(
        """
import json
from pathlib import Path
from types import SimpleNamespace


def load_config(path):
    values = json.loads(Path(path).read_text())
    return SimpleNamespace(
        slow_speed=15, left_drive_trim_pwm=1, right_drive_trim_pwm=-1,
        approach_step_seconds=0.02, forward_interval=lambda distance: 0.02,
        forward_threshold=lambda: 14, **values,
    )


class Hardware:
    def __init__(self, config, emit, stop):
        self.config, self.emit, self.stop = config, emit, stop
        self.moving = False

    def initialize(self):
        self.emit("fake_initialized")
        if self.config.mode == "init_failure":
            raise RuntimeError("Initialization failed")

    def set_maneuver_mode(self, enabled):
        self.emit("fake_slow", enabled=enabled)

    def front_distance(self):
        self.emit("fake_distance", moving=self.moving)
        if self.moving and self.config.mode == "sensor_failure":
            self.moving = False
            return None
        if self.config.mode == "obstacle":
            return 10
        return 50

    def advance(self, seconds):
        self.moving = True
        self.emit("fake_advance", seconds=seconds)
        self.stop.wait(seconds)

    def turn(self, angle):
        self.emit("fake_turn", angle=angle)

    def pulse(self, kind, seconds, speed, direction, *, right_speed):
        self.emit("fake_pulse", kind=kind, seconds=seconds, speed=speed,
                  direction=direction, right_speed=right_speed)
        if self.stop.wait(seconds):
            raise InterruptedError("Stop requested")

    def close(self):
        self.emit("fake_closed")
""",
        encoding="utf-8",
    )
    config = tmp_path / "robot.json"

    def arguments(mode="clear"):
        config.write_text(json.dumps({"mode": mode}), encoding="utf-8")
        return ["--script-dir", str(tmp_path), "--config", str(config)]

    return arguments


def command(direction, arguments=()):
    return [sys.executable, str(WORKER), "--direction", direction, *arguments]


def events(output):
    return [json.loads(line) for line in output.splitlines()]


@pytest.mark.parametrize(("direction", "angle"), [("left", -45), ("right", 45)])
def test_hardware_performs_one_calibrated_turn(fake_driver, direction, angle):
    result = subprocess.run(command(direction, fake_driver()), capture_output=True, text=True)
    records = events(result.stdout)
    assert result.returncode == 0, result.stderr
    assert [record["angle"] for record in records if record["event"] == "fake_turn"] == [angle]
    assert [record["event"] for record in records][-2:] == ["fake_closed", "manual_finished"]


@pytest.mark.parametrize(("mode", "count"), [("obstacle", 0), ("sensor_failure", 1)])
def test_forward_requires_clear_range_and_cleanup(fake_driver, mode, count):
    result = subprocess.run(
        command("forward", fake_driver(mode)), capture_output=True, text=True, timeout=3
    )
    names = [record["event"] for record in events(result.stdout)]
    assert result.returncode == 1
    assert names.count("fake_advance") == count
    assert names[-2:] == ["fake_closed", "manual_failed"]


def test_initialization_failure_still_closes_hardware(fake_driver):
    result = subprocess.run(
        command("forward", fake_driver("init_failure")), capture_output=True, text=True
    )
    names = [record["event"] for record in events(result.stdout)]
    assert result.returncode == 1
    assert "fake_advance" not in names
    assert names[-2:] == ["fake_closed", "manual_failed"]


@pytest.mark.skipif(os.name != "posix", reason="Requires graceful SIGTERM")
@pytest.mark.parametrize("hardware", [True, False])
def test_straight_motion_stops_on_sigterm(fake_driver, hardware):
    args = command("backward", fake_driver() if hardware else ())
    with subprocess.Popen(args, stdout=subprocess.PIPE, text=True) as process:
        while line := process.stdout.readline():
            record = json.loads(line)
            if record["event"] == ("fake_pulse" if hardware else "manual_started"):
                break
        process.terminate()
        records = events(process.communicate(timeout=2)[0])
    assert process.returncode == 0
    assert records[-1]["event"] == "manual_finished"
    assert records[-1]["reason"] == "signal"
    if hardware:
        assert record["direction"] == [0, 1, 0, 1]
        assert (record["speed"], record["right_speed"]) == (16, 14)
        assert records[-2]["event"] == "fake_closed"


def test_straight_mock_stops_at_independent_deadline():
    result = subprocess.run(command("forward"), capture_output=True, text=True, timeout=7)
    assert result.returncode == 0
    assert events(result.stdout)[-1]["reason"] == "time_limit"


def test_agent_worker_waits_until_armed():
    proc = subprocess.Popen(
        [sys.executable, str(WORKER), "--direction", "right", "--wait-for-arm"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
    )
    try:
        stdout, _ = proc.communicate(b"", timeout=3)
        assert proc.returncode == 0
        assert b"manual_started" not in stdout
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
