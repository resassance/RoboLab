import os
import sys
import time
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from agent import camera
from agent.main import create_app
from agent.yahboom import Yahboom, YahboomSettings

TOKEN = "local-camera-test-token-with-32-characters"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
JPEG = b"\xff\xd8mock frame\xff\xd9"


@pytest.fixture
def fake_camera(monkeypatch, tmp_path):
    worker = tmp_path / "camera_worker.py"
    opened = tmp_path / "opened"
    released = tmp_path / "released"
    worker.write_text(
        "import signal, struct, sys, time\n"
        "from pathlib import Path\n"
        f"opened = Path({str(opened)!r})\n"
        f"released = Path({str(released)!r})\n"
        "opened.write_text('started')\n"
        "def stop(*_):\n"
        "    released.write_text('closed')\n"
        "    raise SystemExit(0)\n"
        "signal.signal(signal.SIGTERM, stop)\n"
        f"jpeg = {JPEG!r}\n"
        "while True:\n"
        "    sys.stdout.buffer.write(struct.pack('!I', len(jpeg)) + jpeg)\n"
        "    sys.stdout.buffer.flush()\n"
        "    time.sleep(0.08)\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(camera, "WORKER", worker)
    return worker, opened, released


def start_camera(api):
    response = api.post("/camera", json={"view": "overlay", "fps": 5, "quality": 70})
    assert response.status_code == 201
    camera_id = response.json()["id"]
    return f"/camera/{camera_id}"


def first_frame(api, endpoint):
    for _ in range(5):
        response = api.get(f"{endpoint}/frame", params={"after": 0})
        if response.status_code != 204:
            return response
    pytest.fail("The camera produced no frame")


def test_camera_stays_off_on_connect_and_on_mock_run(fake_camera):
    _, opened, _ = fake_camera
    with TestClient(create_app(TOKEN, mock_camera=True), headers=AUTH) as api:
        assert api.get("/health").json()["camera_available"] is True
        assert api.get("/scripts").is_success
        assert api.get(f"/camera/{uuid4()}/frame").status_code == 404
        run = api.post("/runs", json={"script_id": "mock-robot", "parameters": {"steps": 1}})
        assert run.status_code == 201
        assert not opened.exists()


def test_start_frame_metadata_and_explicit_stop(fake_camera):
    _, opened, released = fake_camera
    with TestClient(create_app(TOKEN, mock_camera=True), headers=AUTH) as api:
        assert api.post("/camera", json={}, headers={"Authorization": ""}).status_code == 401
        assert api.post("/camera", json={"fps": 20}).status_code == 422
        assert not opened.exists()
        endpoint = start_camera(api)
        assert api.post("/camera", json={}).status_code == 409
        frame = first_frame(api, endpoint)
        assert frame.status_code == 200
        assert frame.content == JPEG
        assert frame.headers["content-type"] == "image/jpeg"
        assert frame.headers["cache-control"] == "no-store"
        assert int(frame.headers["x-frame-sequence"]) >= 1
        assert float(frame.headers["x-frame-fps"]) >= 0
        assert int(frame.headers["x-frame-age-ms"]) >= 0
        newer = api.get(f"{endpoint}/frame", params={"after": frame.headers["x-frame-sequence"]})
        assert newer.status_code == 200
        assert int(newer.headers["x-frame-sequence"]) > int(frame.headers["x-frame-sequence"])
        assert api.delete(endpoint).status_code == 204
        if sys.platform != "win32":
            assert released.exists()
        assert api.get(f"{endpoint}/frame").status_code == 404


def test_idle_viewer_releases_worker(monkeypatch, fake_camera):
    _, _, released = fake_camera
    monkeypatch.setattr(camera, "VIEWER_TIMEOUT", 0.6)
    with TestClient(create_app(TOKEN, mock_camera=True), headers=AUTH) as api:
        endpoint = start_camera(api)
        if sys.platform == "win32":
            time.sleep(0.8)
        else:
            deadline = time.monotonic() + 3
            while not released.exists() and time.monotonic() < deadline:
                time.sleep(0.03)
            assert released.exists()
        assert api.get(f"{endpoint}/frame").status_code == 404


def test_invalid_worker_packet_reports_failure(fake_camera):
    worker = fake_camera[0]
    worker.write_text(
        "import struct, sys\n"
        "sys.stdout.buffer.write(struct.pack('!I', 9000000))\n"
        "sys.stdout.buffer.flush()\n",
        encoding="utf-8",
    )
    with TestClient(create_app(TOKEN, mock_camera=True), headers=AUTH) as api:
        endpoint = start_camera(api)
        assert first_frame(api, endpoint).status_code == 503
        assert api.delete(endpoint).status_code == 204


@pytest.mark.skipif(sys.platform != "linux", reason="Yahboom resource lock is Linux-only")
def test_camera_and_yahboom_use_the_same_device_lock(fake_camera, tmp_path, monkeypatch):
    navigation = tmp_path / "robot_navigation.py"
    navigation.write_text("import time; time.sleep(1)\n", encoding="utf-8")
    config_file = tmp_path / "robot.json"
    config_file.write_text("{}", encoding="utf-8")
    hardware = Yahboom(
        YahboomSettings(
            python=Path(sys.executable),
            script_dir=tmp_path,
            config_file=config_file,
            log_dir=tmp_path,
            lock_file=tmp_path / "device.lock",
            camera_device="/dev/video0",
        )
    )

    async def motor_off(self, fd):
        return None

    monkeypatch.setattr(Yahboom, "motor_off", motor_off)
    with TestClient(create_app(TOKEN, hardware), headers=AUTH) as api:
        lock = hardware.acquire()
        try:
            assert api.post("/camera", json={}).status_code == 409
        finally:
            os.close(lock)
        endpoint = start_camera(api)
        assert first_frame(api, endpoint).is_success
        assert (
            api.post(
                "/runs",
                json={
                    "script_id": "yahboom-distance",
                    "supports_lease": True,
                },
            ).status_code
            == 409
        )
        assert api.post("/safety/stop").json()["latched"] is True
        assert api.get(f"{endpoint}/frame").status_code == 404
        assert api.post("/camera", json={}).status_code == 409
        assert api.post("/safety/reset").json()["latched"] is False
        start_camera(api)


@pytest.mark.skipif(sys.platform == "win32", reason="Uses the POSIX SIGTERM handler")
def test_stopping_captures_which_ignore_sigterm(fake_camera):
    worker, opened, _ = fake_camera
    worker.write_text(
        "import os, signal, time\n"
        "from pathlib import Path\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        f"Path({str(opened)!r}).write_text(str(os.getpid()))\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    with TestClient(create_app(TOKEN, mock_camera=True), headers=AUTH) as api:
        endpoint = start_camera(api)
        deadline = time.monotonic() + 3
        while not opened.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert opened.exists()
        pid = int(opened.read_text())
        assert api.delete(endpoint).status_code == 204
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
        assert api.get(f"{endpoint}/frame").status_code == 404


def test_application_shutdown_stops_the_worker(fake_camera):
    _, _, released = fake_camera
    with TestClient(create_app(TOKEN, mock_camera=True), headers=AUTH) as api:
        assert first_frame(api, start_camera(api)).is_success
    if sys.platform != "win32":
        assert released.exists()


def test_stop_timeout_retains_session_until_worker_exits(monkeypatch, fake_camera):
    import asyncio

    original = camera.CameraPreview.finish_process

    async def slow_cleanup(process):
        await asyncio.sleep(0.3)
        await original(process)

    monkeypatch.setattr(camera, "STOP_WAIT", 0.08)
    monkeypatch.setattr(camera.CameraPreview, "finish_process", staticmethod(slow_cleanup))
    with TestClient(create_app(TOKEN, mock_camera=True), headers=AUTH) as api:
        endpoint = start_camera(api)
        assert first_frame(api, endpoint).is_success
        assert api.delete(endpoint).status_code == 503
        assert api.get(f"{endpoint}/frame").status_code == 503
        assert api.post("/camera", json={}).status_code == 409

        deadline = time.monotonic() + 2
        while not fake_camera[2].exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        if sys.platform != "win32":
            assert fake_camera[2].exists()
