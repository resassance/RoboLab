import asyncio
import json
import os
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

fcntl = pytest.importorskip("fcntl")
pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Yahboom requires Linux")


@pytest.fixture
def local_config(tmp_path):
    (tmp_path / "robot_navigation.py").touch()
    (tmp_path / "robot.json").write_text("{}")
    (tmp_path / "logs").mkdir()
    path = tmp_path / "yahboom.json"

    def write(**changes):
        values = {
            "python": sys.executable,
            "script_dir": str(tmp_path),
            "config_file": str(tmp_path / "robot.json"),
            "log_dir": str(tmp_path / "logs"),
            "lock_file": str(tmp_path / "device.lock"),
        }
        path.write_text(json.dumps(values | changes))
        return path

    return write


def test_catalog_limits_distance_and_gates_movement(local_config, tmp_path):
    from agent.yahboom import load_yahboom

    yahboom = load_yahboom(local_config())
    assert yahboom.script_path == tmp_path / "robot_navigation.py"
    assert set(yahboom.catalog) == {"yahboom-distance"}
    assert not (tmp_path / "device.lock").exists()

    distance = yahboom.catalog["yahboom-distance"]
    with pytest.raises(ValueError):
        distance.arguments({"cycles": 21})
    with pytest.raises(ValueError):
        distance.arguments({"max_seconds": 31})
    assert yahboom.arguments(distance.id, distance.arguments({})) == [
        f"--config={tmp_path / 'robot.json'}",
        f"--log-dir={tmp_path / 'logs'}",
        "--mode=distance",
        "--cycles=1",
        "--max-seconds=12.0",
    ]

    yahboom = load_yahboom(local_config(allow_motion=True))
    auto = yahboom.catalog["yahboom-auto"]
    with pytest.raises(ValueError):
        auto.arguments({"obstacle_mode": "camera"})
    with pytest.raises(ValueError):
        auto.arguments({"max_seconds": 21})
    assert yahboom.arguments(auto.id, auto.arguments({}))[-3:] == [
        "--mode=auto",
        "--obstacle-mode=bypass",
        "--max-seconds=10.0",
    ]


@pytest.mark.parametrize(
    "changes",
    [{"python": "python3"}, {"allow_motion": "true"}, {"unexpected": True}],
)
def test_invalid_local_settings_are_rejected(local_config, changes):
    from agent.yahboom import load_yahboom

    with pytest.raises(ValueError):
        load_yahboom(local_config(**changes))


def test_script_cannot_point_outside_trusted_directory(local_config, tmp_path):
    from agent.yahboom import load_yahboom

    filename = local_config()
    source = tmp_path / "elsewhere.py"
    source.touch()
    script = tmp_path / "robot_navigation.py"
    script.unlink()
    script.symlink_to(source)

    with pytest.raises(ValueError):
        load_yahboom(filename)


def test_lock_conflicts_with_manual_flock_and_keeps_inode(local_config, tmp_path):
    from agent.yahboom import DeviceBusy, load_yahboom

    yahboom = load_yahboom(local_config())
    lock = tmp_path / "device.lock"
    with lock.open("wb") as manual:
        fcntl.flock(manual, fcntl.LOCK_EX | fcntl.LOCK_NB)
        inode = lock.stat().st_ino
        with pytest.raises(DeviceBusy):
            yahboom.acquire()

    fd = yahboom.acquire()
    try:
        assert os.fstat(fd).st_ino == inode
        with lock.open("rb") as manual, pytest.raises(BlockingIOError):
            fcntl.flock(manual, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(fd)
    assert lock.stat().st_ino == inode


def test_lock_rejects_symlink_inserted_after_config_load(local_config, tmp_path):
    from agent.yahboom import load_yahboom

    yahboom = load_yahboom(local_config())
    target = tmp_path / "target"
    target.write_text("keep")
    (tmp_path / "device.lock").symlink_to(target)
    with pytest.raises(OSError):
        yahboom.acquire()
    assert target.read_text() == "keep"


@pytest.mark.parametrize(("failure", "expected"), [(None, 0), ("setup", 1), ("readback", 1)])
def test_motor_helper_drives_only_enable_lines_low(monkeypatch, failure, expected):
    from agent.motor_off import main

    calls = []

    def setup(pin, mode, *, initial):
        calls.append(("setup", pin, mode, initial))
        if pin == 16 and failure == "setup":
            raise RuntimeError("simulated setup failure")

    def readback(pin):
        calls.append(("input", pin))
        return int(pin == 16 and failure == "readback")

    gpio = SimpleNamespace(
        BCM=11,
        OUT=0,
        LOW=0,
        setmode=lambda mode: calls.append(("mode", mode)),
        setup=setup,
        input=readback,
    )
    rpi = ModuleType("RPi")
    rpi.GPIO = gpio
    monkeypatch.setitem(sys.modules, "RPi", rpi)
    monkeypatch.setitem(sys.modules, "RPi.GPIO", gpio)
    assert main() == expected
    first_read = [] if failure == "setup" else [("input", 16)]
    assert calls == [
        ("mode", 11),
        ("setup", 16, 0, 0),
        ("setup", 13, 0, 0),
        *first_read,
        ("input", 13),
    ]


@pytest.mark.parametrize("exit_code", [0, 6])
def test_motor_off_runs_local_interpreter_in_isolated_mode(
    local_config, tmp_path, monkeypatch, exit_code
):
    from agent.yahboom import load_yahboom

    python = tmp_path / "external-python"
    received = tmp_path / "received.json"
    python.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "locked = any(p.exists() and p.resolve().name == 'device.lock' for p in Path('/proc/self/fd').iterdir())\n"
        f"with open({str(received)!r}, 'w') as f:\n"
        "    json.dump([sys.argv[1:], 'ROBOLAB_TOKEN' in os.environ, locked], f)\n"
        f"sys.exit({exit_code})\n"
    )
    python.chmod(0o700)
    monkeypatch.setenv("ROBOLAB_TOKEN", "private")
    yahboom = load_yahboom(local_config(python=str(python)))
    lock_fd = yahboom.acquire()
    try:
        if exit_code:
            with pytest.raises(OSError):
                asyncio.run(yahboom.motor_off(lock_fd))
        else:
            asyncio.run(yahboom.motor_off(lock_fd))
    finally:
        os.close(lock_fd)

    argv, token_in_environment, holds_lock = json.loads(received.read_text())
    assert holds_lock is True
    assert argv[:-1] == ["-I"]
    assert Path(argv[-1]).name == "motor_off.py"
    assert Path(argv[-1]).is_absolute()
    assert token_in_environment is False


def test_motor_off_kills_and_reaps_helper_on_timeout(local_config, tmp_path, monkeypatch):
    from agent import yahboom

    python = tmp_path / "external-python"
    child_pid = tmp_path / "child-pid"
    python.write_text(
        f"#!{sys.executable}\n"
        "import os, time\n"
        f"with open({str(child_pid)!r}, 'w') as f:\n"
        "    f.write(str(os.getpid()))\n"
        "time.sleep(60)\n"
    )
    python.chmod(0o700)
    monkeypatch.setattr(yahboom, "MOTOR_OFF_TIMEOUT", 0.5)
    adapter = yahboom.load_yahboom(local_config(python=str(python)))
    lock_fd = adapter.acquire()
    try:
        with pytest.raises(OSError, match="Истекло время"):
            asyncio.run(adapter.motor_off(lock_fd))
    finally:
        os.close(lock_fd)

    with pytest.raises(ProcessLookupError):
        os.kill(int(child_pid.read_text()), 0)
