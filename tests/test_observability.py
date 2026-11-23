import csv
import io
import json
import sys
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from agent.main import create_app

TOKEN = "local-metrics-test-token-with-32-characters"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def finish(api, parameters=None):
    with api.websocket_connect("/events") as events:
        response = api.post(
            "/runs", json={"script_id": "mock-robot", "parameters": parameters or {}}
        )
        assert response.status_code == 201, response.text
        run_id = response.json()["id"]
        for _ in range(16):
            frame = events.receive_json()
            if (
                frame["run"]
                and frame["run"]["id"] == run_id
                and frame["run"]["exit_code"] is not None
            ):
                return run_id
    raise AssertionError("The experiment did not finish")


def test_experiment_survives_restart_and_exports_logs_and_metrics(tmp_path):
    data_dir = tmp_path / "state"
    params = {"steps": 3, "interval": 0.1, "label": "Пробный заезд"}
    with TestClient(create_app(TOKEN, data_dir=data_dir), headers=AUTH) as api:
        run_id = finish(api, params)
        assert api.get("/health").json()["observability_available"] is True
        summaries = api.get("/history").json()
        assert summaries[0]["id"] == run_id
        assert summaries[0]["script_name"] == "Mock Robot"
        assert summaries[0]["status"] == "succeeded"
        assert summaries[0]["metrics"]["step"] == 3

    with TestClient(create_app(TOKEN, data_dir=data_dir), headers=AUTH) as api:
        details = api.get(f"/history/{run_id}").json()
        assert details["run"]["parameters"]["label"] == "Пробный заезд"
        assert details["run"]["parameters"]["steps"] == 3
        assert details["run"]["finished_at"] is not None
        assert details["run"]["exit_code"] == 0
        assert [sample["value"] for sample in details["samples"] if sample["name"] == "step"] == [
            1,
            2,
            3,
        ]
        assert any("Пробный заезд" in message["message"] for message in details["logs"])
        export = api.get(f"/history/{run_id}/export", params={"format": "json"})
        assert export.json() == details
        assert export.headers["content-disposition"].endswith('.json"')
        csv_export = api.get(f"/history/{run_id}/export", params={"format": "csv"})
        assert csv_export.status_code == 200
        assert csv_export.headers["content-type"].startswith("text/csv")
        rows = list(csv.DictReader(io.StringIO(csv_export.content.decode("utf-8-sig"))))
        assert len(rows) >= len(details["logs"]) + len(details["samples"])
        assert "Пробный заезд" in csv_export.text
        assert api.get(f"/history/{uuid4()}").status_code == 404


def test_history_and_telemetry_authenticated_with_no_hardware_writes():
    with TestClient(create_app(TOKEN)) as client:
        assert client.get("/history").status_code == 401
        assert client.get("/telemetry").status_code == 401
        assert client.get("/history", headers=AUTH).json() == []
        response = client.get("/telemetry", headers=AUTH)
        assert response.status_code == 200
        data = response.json()
        assert data["robot"] == "mock"
        assert data["camera_available"] is False
        assert data["hardware_busy"] is False
        assert data["run_id"] is None
        assert data["timestamp"]
        if sys.platform == "linux" and Path("/proc/meminfo").is_file():
            assert data["ram_total_bytes"] > 0
            assert 0 <= data["ram_used_bytes"] <= data["ram_total_bytes"]


def test_metrics_reject_unbounded_values_and_keep_finite_numbers(tmp_path, monkeypatch):
    from agent.main import MOCK, REGISTRY

    script = tmp_path / "example.py"
    weird = json.dumps(
        {
            "message": "metrics",
            "metrics": {"good": 1.5, "invalid": "1", "nan": float("nan"), "bool": True},
        }
    )
    script.write_text(f"print({weird!r})\n", encoding="utf-8")
    monkeypatch.setitem(REGISTRY, MOCK.id, (MOCK, script))
    with TestClient(create_app(TOKEN), headers=AUTH) as api:
        run_id = finish(api)
        data = api.get(f"/history/{run_id}").json()
        assert data["run"]["metrics"] == {"good": 1.5}
        assert [sample["name"] for sample in data["samples"]] == ["good"]
        assert api.get("/telemetry").json()["metrics"] == {"good": 1.5}


def test_linux_telemetry_uses_proc_deltas_and_optional_thermal_sensor(tmp_path):
    import asyncio

    from agent.telemetry import TelemetrySampler

    proc = tmp_path / "proc"
    (proc / "net").mkdir(parents=True)
    (proc / "stat").write_text("cpu  100 0 100 800 0 0 0 0 5 0\n")
    (proc / "meminfo").write_text("MemTotal: 1000 kB\nMemAvailable: 600 kB\n")
    (proc / "net" / "dev").write_text(
        "lo: 100 0 0 0 0 0 0 0 200 0 0 0 0 0 0 0\neth0: 12 0 0 0 0 0 0 0 34 0 0 0 0 0 0 0\n"
    )
    thermal = tmp_path / "thermal"
    sensor = thermal / "thermal_zone0" / "temp"
    sensor.parent.mkdir(parents=True)
    sensor.write_text("42500\n")
    network = tmp_path / "network"
    state = network / "eth0" / "operstate"
    state.parent.mkdir(parents=True)
    state.write_text("up\n")

    async def exercise():
        sampler = TelemetrySampler(proc, thermal, network)
        first = await sampler.sample()
        assert first.cpu_percent is None
        assert first.ram_total_bytes == 1000 * 1024
        assert first.ram_used_bytes == 400 * 1024
        assert first.temperature_c == 42.5
        assert first.interfaces == ["eth0"]
        assert first.network_operational is True
        assert (first.network_rx_bytes, first.network_tx_bytes) == (12, 34)
        (proc / "stat").write_text("cpu  150 0 125 825 0 0 0 0 5 0\n")
        sensor.unlink()
        state.write_text("down\n")
        sampler.sampled_at = 0.0
        second = await sampler.sample()
        assert second.cpu_percent == 75.0
        assert second.temperature_c is None
        assert second.network_operational is False

    asyncio.run(exercise())
