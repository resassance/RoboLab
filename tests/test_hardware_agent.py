import os
import signal
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from agent import main, supervisor
from agent.main import create_app
from agent.yahboom import Yahboom, YahboomSettings

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Yahboom uses Linux process groups")

TOKEN = "local-hardware-test-token-of-32-characters"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}
START = {"script_id": "yahboom-distance", "supports_lease": True}


@pytest.fixture
def hardware(tmp_path, monkeypatch):
    navigation = tmp_path / "robot_navigation.py"
    navigation.write_text(
        "import signal, sys, time\n"
        "signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))\n"
        "print(repr(sys.argv), flush=True)\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    config = tmp_path / "config.json"
    config.write_text("{}", encoding="utf-8")
    settings = YahboomSettings(
        python=Path(sys.executable),
        script_dir=tmp_path,
        config_file=config,
        log_dir=tmp_path,
        lock_file=tmp_path / "hardware.lock",
    )
    calls = []

    async def disable_motors(self, lock_fd):
        calls.append("motor_off")

    monkeypatch.setattr(Yahboom, "motor_off", disable_motors)
    monkeypatch.setattr(supervisor, "HEARTBEAT_TIMEOUT", 0.45)
    return Yahboom(settings), calls


@pytest.fixture
def api(hardware):
    adapter, calls = hardware
    with TestClient(create_app(TOKEN, adapter), headers=HEADERS) as client:
        yield client


def receive_until(events, condition):
    for _ in range(12):
        packet = events.receive_json()
        if condition(packet):
            return packet
    pytest.fail("Expected state did not arrive")


def test_hardware_is_opt_in_and_uses_a_private_lease(api, hardware, monkeypatch):
    _, calls = hardware
    monkeypatch.setattr(supervisor, "HEARTBEAT_TIMEOUT", 4.0)
    health = api.get("/health").json()
    assert health["robot"] == "yahboom"
    assert health.get("manual_available", False) is False
    assert api.post("/manual", json={"direction": "forward"}).status_code == 404
    scripts = {script["id"]: script for script in api.get("/scripts").json()}
    assert scripts["yahboom-distance"]["requires_heartbeat"] is True
    assert "yahboom-auto" not in scripts
    assert api.post("/runs", json={"script_id": "yahboom-distance"}).status_code == 422
    assert calls == []

    with api.websocket_connect("/events") as events:
        started = api.post("/runs", json=START)
        assert started.status_code == 201
        value = started.json()
        run_id = value["id"]
        assert value["lease"]
        assert calls == ["motor_off"]
        frame = receive_until(events, lambda packet: packet["logs"])
        assert "lease" not in frame["run"]
        assert frame["run"]["id"] == run_id
        assert "--mode=distance" in frame["logs"][0]["message"]
        assert (
            api.post(f"/runs/{run_id}/heartbeat", json={"lease": str(uuid4())}).status_code == 409
        )
        assert (
            api.post(f"/runs/{run_id}/heartbeat", json={"lease": value["lease"]}).status_code == 204
        )
        assert api.post(f"/runs/{run_id}/stop").status_code == 200
        packet = receive_until(events, lambda packet: packet["run"]["exit_code"] is not None)
        assert packet["run"]["status"] == "stopped"
        assert packet["safety"]["motor_disable_confirmed"] is True
        assert calls == ["motor_off", "motor_off"]


def test_heartbeat_timeout_stops_the_process_and_latches(api, hardware):
    with api.websocket_connect("/events") as events:
        started = api.post("/runs", json=START).json()
        packet = receive_until(
            events, lambda p: p["run"] is not None and p["run"]["status"] == "stopped"
        )
        assert packet["safety"]["latched"] is True
        assert packet["safety"]["motor_disable_confirmed"] is True
        assert api.post("/runs", json=START).status_code == 409
        assert (
            api.post(
                f"/runs/{started['id']}/heartbeat", json={"lease": started["lease"]}
            ).status_code
            == 409
        )
        assert api.post("/safety/reset").json()["latched"] is False
        assert hardware[1] == ["motor_off", "motor_off", "motor_off"]


def test_explicit_emergency_stop_holds_latch_until_reset(api, hardware):
    started = api.post("/runs", json=START).json()
    assert api.post("/safety/reset").status_code == 409
    assert api.post("/safety/stop", headers={"Authorization": ""}).status_code == 401
    stop = api.post("/safety/stop")
    assert stop.status_code == 200
    assert stop.json()["latched"] is True
    assert stop.json()["motor_disable_confirmed"] is True
    assert api.post("/runs", json=START).status_code == 409
    with api.websocket_connect("/events") as events:
        frame = events.receive_json()
        assert frame["run"]["id"] == started["id"]
        assert frame["run"]["status"] == "stopped"
    assert api.post("/safety/reset").status_code == 200
    assert hardware[1] == ["motor_off", "motor_off", "motor_off"]


def test_prepare_poweroff_holds_reset_until_latest_request_expires(api, monkeypatch):
    now = 100.0
    monkeypatch.setattr(main, "monotonic", lambda: now)
    prepare = "/safety/prepare-poweroff"

    assert api.post(prepare, headers={"Authorization": ""}).status_code == 401
    assert api.post("/safety/reset").status_code == 200
    result = api.post(prepare)
    assert result.status_code == 200
    assert result.json()["latched"] is True
    assert result.json()["motor_disable_confirmed"] is True
    assert api.post("/safety/reset").status_code == 409
    assert api.post("/runs", json=START).status_code == 409

    now = 120.0
    assert api.post(prepare).status_code == 200
    now = 130.0
    assert api.post("/safety/reset").status_code == 409
    now = 149.9
    assert api.post("/safety/reset").status_code == 409
    now = 150.0
    assert api.post("/runs", json=START).status_code == 409
    assert api.post("/safety/reset").json()["latched"] is False


def test_prepare_poweroff_holds_reset_before_cutoff_and_after_disconnect(hardware, monkeypatch):
    import asyncio

    import httpx

    async def exercise():
        cutoff_started = asyncio.Event()

        async def suspend_cutoff(self, lock_fd):
            cutoff_started.set()
            await asyncio.Future()

        monkeypatch.setattr(Yahboom, "motor_off", suspend_cutoff)
        transport = httpx.ASGITransport(app=create_app(TOKEN, hardware[0]))
        async with (
            httpx.AsyncClient(
                transport=transport, base_url="http://robot", headers=HEADERS
            ) as user,
            httpx.AsyncClient(
                transport=transport, base_url="http://robot", headers=HEADERS
            ) as other,
        ):
            preparing = asyncio.create_task(user.post("/safety/prepare-poweroff"))
            try:
                await asyncio.wait_for(cutoff_started.wait(), timeout=2)
                reset = await asyncio.wait_for(other.post("/safety/reset"), timeout=2)
                assert reset.status_code == 409
            finally:
                preparing.cancel()
                await asyncio.gather(preparing, return_exceptions=True)
            reset = await asyncio.wait_for(other.post("/safety/reset"), timeout=2)
            assert reset.status_code == 409

    asyncio.run(exercise())


def test_reset_queued_before_new_safety_latch_is_rejected(hardware):
    import asyncio

    async def exercise():
        controller = supervisor.Supervisor(hardware[0])
        async with controller.start_lock:
            reset = asyncio.create_task(controller.reset_safety())
            await asyncio.sleep(0)
            controller.inhibit("Запрошена аварийная остановка.")
        with pytest.raises(supervisor.RunConflict):
            await reset
        assert controller.safety.latched

    asyncio.run(exercise())


def test_external_lock_prevents_start_and_cutoff(api, hardware):
    adapter, calls = hardware
    lock = adapter.acquire()
    try:
        assert api.post("/runs", json=START).status_code == 409
        assert api.post("/safety/stop").status_code == 409
        assert calls == []
        with api.websocket_connect("/events") as events:
            safety = events.receive_json()["safety"]
            assert safety["latched"] is True
            assert safety["motor_disable_confirmed"] is not True
    finally:
        os.close(lock)

    assert api.post("/safety/reset").status_code == 200
    assert calls == ["motor_off"]


def test_cutoff_failure_latches_and_denies_launch(api, monkeypatch):
    async def blocked(self, lock_fd):
        raise PermissionError("synthetic GPIO access denied")

    monkeypatch.setattr(Yahboom, "motor_off", blocked)
    assert api.post("/runs", json=START).status_code == 503
    with api.websocket_connect("/events") as events:
        safety = events.receive_json()["safety"]
        assert safety["latched"] is True
        assert safety["motor_disable_confirmed"] is False
    assert api.post("/safety/reset").status_code == 503
    assert api.post("/runs", json=START).status_code == 409


def test_operating_limit_applies_while_client_renews_lease(api, hardware, monkeypatch):
    hardware[0].script_path.write_text(
        "import signal, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "print('started', flush=True)\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(supervisor, "HEARTBEAT_TIMEOUT", 3.0)
    response = api.post("/runs", json={**START, "parameters": {"max_seconds": 1}})
    assert response.status_code == 201
    run = response.json()
    path = f"/runs/{run['id']}/heartbeat"
    assert api.post(path, json={"lease": run["lease"]}).status_code == 204
    with api.websocket_connect("/events") as events:
        packet = receive_until(events, lambda p: p["run"]["status"] == "stopped")
        assert packet["safety"]["latched"] is False
        assert packet["safety"]["motor_disable_confirmed"] is True
        assert packet["run"]["exit_code"] == -signal.SIGKILL
    with api.websocket_connect("/events") as events:
        assert any("лимит времени" in log["message"] for log in events.receive_json()["logs"])
    assert api.post(path, json={"lease": run["lease"]}).status_code == 409


def test_process_group_is_stopped_before_cutoff(api, hardware, monkeypatch):
    adapter, calls = hardware
    adapter.script_path.write_text(
        "import subprocess, sys\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
        "print(child.pid, flush=True)\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(supervisor, "HEARTBEAT_TIMEOUT", 3.0)
    with api.websocket_connect("/events") as events:
        assert api.post("/runs", json=START).status_code == 201
        lines = []
        for _ in range(12):
            packet = events.receive_json()
            lines.extend(packet["logs"])
            if packet["run"] and packet["run"]["status"] == "succeeded":
                break
        assert packet["run"]["status"] == "succeeded"
        child_pid = int(lines[0]["message"])
        assert calls == ["motor_off", "motor_off"]
        stat = Path(f"/proc/{child_pid}/stat")
        try:
            child_state = stat.read_text().split(") ", 1)[1][0]
        except FileNotFoundError:
            child_state = None
        assert child_state in (None, "Z")


def test_cutoff_failure_during_stop_is_reported(api, hardware, monkeypatch):
    _, calls = hardware

    async def fails_after_start(self, lock_fd):
        calls.append("motor_off")
        if len(calls) > 1:
            raise PermissionError("synthetic GPIO access denied during stop")

    monkeypatch.setattr(Yahboom, "motor_off", fails_after_start)
    response = api.post("/runs", json=START)
    assert response.status_code == 201
    assert api.post("/safety/stop").status_code == 503
    with api.websocket_connect("/events") as events:
        packet = events.receive_json()
        assert packet["run"]["status"] == "failed"
        assert packet["safety"]["latched"] is True
        assert packet["safety"]["motor_disable_confirmed"] is False
    assert api.post("/runs", json=START).status_code == 409


def test_emergency_request_timeout_preserves_the_hardware_lock(api, hardware, monkeypatch):
    import asyncio

    from agent.yahboom import DeviceBusy

    adapter, calls = hardware

    async def slow_after_start(self, lock_fd):
        calls.append("motor_off")
        if len(calls) > 1:
            await asyncio.sleep(0.35)

    monkeypatch.setattr(Yahboom, "motor_off", slow_after_start)
    monkeypatch.setattr(supervisor, "EMERGENCY_TIMEOUT", 0.08)
    assert api.post("/runs", json=START).status_code == 201
    assert api.post("/safety/stop").status_code == 503
    with pytest.raises(DeviceBusy):
        adapter.acquire()
    assert api.post("/safety/reset").status_code == 409

    with api.websocket_connect("/events") as events:
        packet = receive_until(events, lambda p: p["run"]["status"] == "stopped")
        assert packet["safety"]["latched"] is True
        assert packet["safety"]["motor_disable_confirmed"] is True


def test_emergency_prevents_spawn_during_preflight(api, hardware, monkeypatch):
    import asyncio
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    _, calls = hardware
    entered = Event()
    proceed = Event()

    async def during_preflight(self, lock_fd):
        calls.append("motor_off")
        if len(calls) == 1:
            entered.set()
            while not proceed.is_set():
                await asyncio.sleep(0.01)

    monkeypatch.setattr(Yahboom, "motor_off", during_preflight)
    with ThreadPoolExecutor(max_workers=2) as pool, api.websocket_connect("/events") as events:
        starting = pool.submit(api.post, "/runs", json=START)
        try:
            assert entered.wait(3)
            stopping = pool.submit(api.post, "/safety/stop")
            packet = receive_until(events, lambda p: p["safety"]["latched"])
            assert packet["run"] is None
        finally:
            proceed.set()
        assert starting.result(timeout=3).status_code == 409
        assert stopping.result(timeout=3).json()["motor_disable_confirmed"] is True
    with api.websocket_connect("/events") as events:
        assert events.receive_json()["run"] is None


def test_reader_failure_does_not_skip_cutoff(api, hardware, monkeypatch):
    from agent.supervisor import Supervisor

    original = Supervisor.drain

    async def broken(self, run, stream, source):
        if source == "stdout":
            raise ValueError("synthetic log reader failure")
        await original(self, run, stream, source)

    monkeypatch.setattr(Supervisor, "drain", broken)
    adapter, calls = hardware
    adapter.script_path.write_text("print('done', flush=True)\n", encoding="utf-8")
    with api.websocket_connect("/events") as events:
        assert api.post("/runs", json=START).status_code == 201
        packet = receive_until(events, lambda p: p["run"] and p["run"]["status"] == "failed")
        assert packet["safety"]["latched"] is True
        assert packet["safety"]["motor_disable_confirmed"] is True
    assert calls == ["motor_off", "motor_off"]


def test_malformed_unicode_in_child_output_is_replayable(api, hardware, monkeypatch):
    monkeypatch.setattr(supervisor, "HEARTBEAT_TIMEOUT", 3.0)
    script = hardware[0].script_path
    script.write_text(
        'print(r\'{"message":"diagnostic \\ud800","event":"\\ud800"}\', flush=True)\n',
        encoding="utf-8",
    )
    response = api.post("/runs", json=START)
    assert response.status_code == 201
    with api.websocket_connect("/events") as events:
        receive_until(events, lambda p: p["run"] and p["run"]["status"] == "succeeded")
    with api.websocket_connect("/events") as events:
        line = events.receive_json()["logs"][0]
        assert line["message"] == "diagnostic ?"
        assert line["event"] == "?"


def test_cancelling_shutdown_does_not_cancel_the_cutoff(hardware, monkeypatch):
    import asyncio

    from agent.supervisor import Supervisor
    from agent.yahboom import DeviceBusy

    adapter, calls = hardware
    adapter.script_path.write_text("print('done', flush=True)\n", encoding="utf-8")

    async def exercise():
        entered = asyncio.Event()
        proceed = asyncio.Event()

        async def delayed_cutoff(self, lock_fd):
            calls.append("motor_off")
            if len(calls) > 1:
                entered.set()
                await proceed.wait()

        monkeypatch.setattr(Yahboom, "motor_off", delayed_cutoff)
        controller = Supervisor(adapter)
        await controller.start("yahboom-distance", adapter.script_path, [], hardware_seconds=3)
        await asyncio.wait_for(entered.wait(), timeout=3)
        closing = asyncio.create_task(controller.close())
        await asyncio.sleep(0)
        closing.cancel()
        await asyncio.sleep(0)
        assert not closing.done()
        with pytest.raises(DeviceBusy):
            adapter.acquire()
        proceed.set()
        with pytest.raises(asyncio.CancelledError):
            await closing
        assert controller.active_run is None
        assert controller.safety.motor_disable_confirmed is True
        os.close(adapter.acquire())

    asyncio.run(exercise())
