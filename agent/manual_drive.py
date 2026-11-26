import argparse
import importlib.util
import json
import os
import signal
import sys
from pathlib import Path
from time import monotonic, sleep

MAX_SECONDS = 5.0
MOCK_TURN_SECONDS = 0.75
TURN_DEGREES = {"left": -45, "right": 45}
DIRECTIONS = {
    "forward": "Вперёд",
    "backward": "Назад",
    "left": "Влево на 45°",
    "right": "Вправо на 45°",
}


class MotionDeadline:
    def __init__(self):
        self.deadline = monotonic() + MAX_SECONDS
        self.signalled = False

    def signal_stop(self, signum, frame):
        self.signalled = True

    def is_set(self):
        return self.signalled or monotonic() >= self.deadline

    def wait(self, seconds):
        end = min(self.deadline, monotonic() + seconds)
        while not self.is_set():
            remaining = end - monotonic()
            if remaining <= 0:
                return False
            sleep(min(0.02, remaining))
        return True


def emit(event, message=None, level="INFO", **details):
    print(
        json.dumps({"event": event, "message": message or event, "level": level, **details}),
        flush=True,
    )


def forward(hardware, stop):
    hardware.set_maneuver_mode(True)
    threshold = hardware.config.forward_threshold()
    distance = hardware.front_distance()
    while not stop.is_set():
        if distance is None or not distance > threshold:
            raise RuntimeError("Дальномер не подтвердил свободный путь вперёд.")
        hardware.advance(hardware.config.forward_interval(distance))
        if not stop.is_set():
            distance = hardware.front_distance()


def drive(script_dir, config_path, direction, stop):
    sys.path.insert(0, str(script_dir))
    spec = importlib.util.spec_from_file_location(
        "robot_navigation", script_dir / "robot_navigation.py"
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("Не удалось открыть драйвер Yahboom.")
    driver = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = driver
    spec.loader.exec_module(driver)
    required_methods = {
        "forward": ("advance", "front_distance", "set_maneuver_mode"),
        "backward": ("pulse",),
        "left": ("turn",),
        "right": ("turn",),
    }
    if not callable(getattr(driver, "load_config", None)) or not all(
        callable(getattr(getattr(driver, "Hardware", None), name, None))
        for name in ("initialize", "close", *required_methods[direction])
    ):
        raise RuntimeError("Установленный драйвер Yahboom не поддерживает эту команду.")

    config = driver.load_config(config_path)
    hardware = driver.Hardware(config, emit, stop)
    try:
        hardware.initialize()
        if stop.is_set():
            return
        if direction in TURN_DEGREES:
            hardware.turn(TURN_DEGREES[direction])
        elif direction == "forward":
            forward(hardware, stop)
        else:
            hardware.pulse(
                "backward",
                MAX_SECONDS,
                config.slow_speed + config.left_drive_trim_pwm,
                (0, 1, 0, 1),
                right_speed=config.slow_speed + config.right_drive_trim_pwm,
            )
    except InterruptedError:
        if not stop.is_set():
            raise
    finally:
        hardware.close()


def main():
    parser = argparse.ArgumentParser(description="Ручное движение RoboLab", allow_abbrev=False)
    parser.add_argument("--direction", required=True, choices=DIRECTIONS)
    parser.add_argument("--script-dir", type=Path)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--wait-for-arm", action="store_true")
    args = parser.parse_args()
    if (args.script_dir is None) != (args.config is None):
        parser.error("Укажите --script-dir и --config вместе.")

    stop = MotionDeadline()
    signal.signal(signal.SIGINT, stop.signal_stop)
    signal.signal(signal.SIGTERM, stop.signal_stop)
    if args.wait_for_arm and (os.read(sys.stdin.fileno(), 1) != b"1" or stop.is_set()):
        return 0
    emit("manual_started", DIRECTIONS[args.direction], direction=args.direction)
    try:
        if args.script_dir is None:
            stop.wait(MOCK_TURN_SECONDS if args.direction in TURN_DEGREES else MAX_SECONDS)
        else:
            drive(args.script_dir, args.config, args.direction, stop)
    except Exception as exc:
        emit("manual_failed", str(exc) or type(exc).__name__, level="ERROR")
        return 1
    reason = "signal" if stop.signalled else "time_limit" if stop.is_set() else "complete"
    emit("manual_finished", "Движение завершено.", reason=reason)
    return 0


if __name__ == "__main__":
    sys.exit(main())
