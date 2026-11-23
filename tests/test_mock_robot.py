import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

MOCK_ROBOT = Path(__file__).resolve().parents[1] / "examples" / "mock_robot.py"


def run_mock(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(MOCK_ROBOT), *args],
        capture_output=True,
        encoding="utf-8",
        env={**os.environ, "PYTHONIOENCODING": "cp1252"},
        timeout=5,
    )


def test_patrol_uses_the_requested_number_of_steps():
    result = run_mock("--steps", "2", "--interval", "0.1")

    assert result.returncode == 0, result.stderr
    logs = [json.loads(line) for line in result.stdout.splitlines()]
    assert [log["event"] for log in logs] == [
        "mock_started",
        "mock_tick",
        "mock_tick",
        "mock_complete",
    ]
    assert "RoboLab" in logs[0]["message"]
    assert "patrol" in logs[0]["message"]
    assert logs[1]["message"] == "Шаг симуляции 1."
    assert logs[2]["message"] == "Шаг симуляции 2."


@pytest.mark.parametrize(("scan_args", "point_count"), [((), 3), (("--scan-points", "6"), 6)])
def test_scan_reports_point_count(scan_args, point_count):
    result = run_mock(
        "--scenario",
        "scan",
        "--label",
        "Стенд",
        "--steps",
        "1",
        "--interval",
        "0.1",
        "--verbose",
        *scan_args,
    )

    assert result.returncode == 0, result.stderr
    logs = [json.loads(line) for line in result.stdout.splitlines()]
    assert "Стенд" in logs[0]["message"]
    assert "scan" in logs[0]["message"]
    assert any(log["level"] == "DEBUG" for log in logs)
    tick = next(log for log in logs if log["event"] == "mock_tick")
    assert "Шаг симуляции" in tick["message"]
    assert str(point_count) in tick["message"]


@pytest.mark.parametrize(
    ("args", "argument"),
    [
        (("--steps", "0"), "--steps"),
        (("--steps", "121"), "--steps"),
        (("--interval", "0.09"), "--interval"),
        (("--interval", "2.01"), "--interval"),
        (("--interval", "nan"), "--interval"),
        (("--interval", "inf"), "--interval"),
        (("--scenario", "invalid"), "--scenario"),
        (("--scan-points", "3"), "--scan-points"),
        (("--scenario", "scan", "--scan-points", "0"), "--scan-points"),
        (("--scenario", "scan", "--scan-points", "9"), "--scan-points"),
        (("--label", " \t"), "--label"),
        (("--label", "x" * 81), "--label"),
    ],
)
def test_invalid_arguments_are_rejected_before_simulation(args, argument):
    result = run_mock(*args)

    assert result.returncode == 2
    assert result.stdout == ""
    assert argument in result.stderr
