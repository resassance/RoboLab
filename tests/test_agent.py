import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from agent.main import create_app

TOKEN = "local-test-token-with-at-least-32-characters"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def api():
    with TestClient(create_app(TOKEN), headers=AUTH) as client:
        yield client


def test_requires_token_for_http_and_websocket():
    with TestClient(create_app(TOKEN)) as api:
        assert api.get("/health").status_code == 401
        assert (
            api.get("/scripts", headers={"Authorization": "Bearer an-incorrect-token"}).status_code
            == 401
        )
        assert api.post("/runs", json={"script_id": "mock-robot"}).status_code == 401
        with pytest.raises(WebSocketDisconnect) as denied, api.websocket_connect("/events"):
            pass
        assert denied.value.code == 1008


def test_registry_lists_only_original_example_and_rejects_other_commands(api):
    assert api.get("/health").json() == {
        "protocol": 1,
        "robot": "mock",
        "observability_available": True,
    }
    catalog = api.get("/scripts").json()
    assert [script["id"] for script in catalog] == ["mock-robot"]
    assert catalog[0]["description"]
    assert api.post("/runs", json={"script_id": "other"}).status_code == 404
    assert (
        api.post("/runs", json={"script_id": "mock-robot", "args": ["--extra"]}).status_code == 422
    )


def test_start_stream_poll_reconnect_and_stop(api):
    with api.websocket_connect("/events") as events:
        assert events.receive_json()["run"] is None
        response = api.post("/runs", json={"script_id": "mock-robot"})
        assert response.status_code == 201
        run = response.json()
        run_id = run["id"]
        assert run["pid"] > 0
        assert run["status"] == "running"
        assert api.post("/runs", json={"script_id": "mock-robot"}).status_code == 409

        logs = []
        while not any(line["source"] == "stderr" for line in logs):
            packet = events.receive_json()
            assert packet["run"]["id"] == run_id
            logs.extend(packet["logs"])
        assert any(line["event"] == "mock_started" for line in logs)
        assert all(line["timestamp"] and line["level"] for line in logs)
        cursor = logs[-1]["sequence"]

    with api.websocket_connect(f"/events?run_id={run_id}&after={cursor}") as events:
        snapshot = events.receive_json()
        assert snapshot["run"]["id"] == run_id
        assert all(log["sequence"] > cursor for log in snapshot["logs"])
        assert api.post("/runs/00000000-0000-0000-0000-000000000001/stop").status_code == 404
        assert api.post(f"/runs/{run_id}/stop").status_code == 200
        final_logs = list(snapshot["logs"])
        while True:
            packet = events.receive_json()
            final_logs.extend(packet["logs"])
            if packet["run"]["status"] == "stopped":
                break
        assert packet["run"]["exit_code"] == 0
        assert packet["run"]["elapsed_seconds"] >= 0
        assert any(line["event"] == "mock_stopped" for line in final_logs)
        assert api.post(f"/runs/{run_id}/stop").json()["status"] == "stopped"

    new_run = api.post("/runs", json={"script_id": "mock-robot"}).json()
    assert new_run["id"] != run_id
    assert api.post(f"/runs/{run_id}/stop").status_code == 404
    with api.websocket_connect(f"/events?run_id={run_id}&after={cursor}") as events:
        assert events.receive_json()["run"]["id"] == new_run["id"]


def test_concurrent_start_allows_only_one_run(api):
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=2) as workers:
        results = workers.map(
            lambda _: api.post("/runs", json={"script_id": "mock-robot"}), range(2)
        )
        assert sorted(result.status_code for result in results) == [201, 409]


def test_run_failures_are_visible(api, monkeypatch):
    from agent import supervisor

    async def spawn_failure(*args, **kwargs):
        raise FileNotFoundError("simulated test error")

    monkeypatch.setattr(supervisor.asyncio, "create_subprocess_exec", spawn_failure)
    assert api.post("/runs", json={"script_id": "mock-robot"}).status_code == 503
    with api.websocket_connect("/events") as events:
        packet = events.receive_json()
        assert packet["run"]["status"] == "failed"
        assert packet["run"]["pid"] is None
        assert packet["logs"][0]["source"] == "agent"


@pytest.mark.parametrize(("code", "status"), [(0, "succeeded"), (17, "failed")])
def test_natural_exit_reports_status_and_exit_code(api, monkeypatch, tmp_path, code, status):
    from agent.main import MOCK, REGISTRY

    script = tmp_path / "example.py"
    script.write_text(f"print('before exit')\nraise SystemExit({code})\n", encoding="utf-8")
    monkeypatch.setitem(REGISTRY, MOCK.id, (MOCK, script))
    response = api.post("/runs", json={"script_id": MOCK.id})
    assert response.status_code == 201

    with api.websocket_connect("/events") as events:
        logs = []
        for _ in range(5):
            packet = events.receive_json()
            logs.extend(packet["logs"])
            if packet["run"]["exit_code"] is not None:
                break
        assert packet["run"]["status"] == status
        assert packet["run"]["exit_code"] == code
        assert [line["message"] for line in logs] == ["before exit"]


def test_reconnect_reports_gap_in_bounded_log(api, monkeypatch, tmp_path):
    from agent.main import MOCK, REGISTRY
    from common.protocol import LOG_LIMIT

    script = tmp_path / "example.py"
    script.write_text(f"for i in range({LOG_LIMIT + 5}):\n    print(i)\n", encoding="utf-8")
    monkeypatch.setitem(REGISTRY, MOCK.id, (MOCK, script))
    run = api.post("/runs", json={"script_id": MOCK.id}).json()

    with api.websocket_connect("/events") as events:
        for _ in range(LOG_LIMIT + 10):
            if events.receive_json()["run"]["status"] == "succeeded":
                break
        else:
            pytest.fail("The example did not complete")

    with api.websocket_connect(f"/events?run_id={run['id']}&after=1") as events:
        snapshot = events.receive_json()
        assert snapshot["gap"] is True
        assert len(snapshot["logs"]) == LOG_LIMIT
        assert snapshot["logs"][0]["sequence"] == 6


def test_catalog_describes_configuration_and_preserves_its_types(api):
    script = api.get("/scripts").json()[0]
    parameters = {parameter["key"]: parameter for parameter in script["parameters"]}
    assert {item["kind"] for item in parameters.values()} == {
        "bool",
        "int",
        "float",
        "string",
        "enum",
    }
    assert parameters["scan_points"]["requires"] == {"scenario": "scan"}
    assert parameters["steps"]["minimum"] == 1
    assert parameters["steps"]["maximum"] == 120


@pytest.mark.parametrize(
    "parameters",
    [
        {"steps": True},
        {"steps": "2"},
        {"steps": 121},
        {"interval": 0.01},
        {"scenario": "other"},
        {"scan_points": 4},
        {"verbose": 1},
        {"unknown": True},
        {"label": " "},
        {"label": "bad\nlabel"},
    ],
)
def test_invalid_configuration_is_rejected_before_start(api, parameters):
    assert (
        api.post("/runs", json={"script_id": "mock-robot", "parameters": parameters}).status_code
        == 422
    )
    with api.websocket_connect("/events") as events:
        assert events.receive_json()["run"] is None


def test_configuration_reaches_mock_process(api):
    options = {
        "steps": 2,
        "interval": 0.1,
        "scenario": "scan",
        "scan_points": 4,
        "label": "--tag for an experiment",
        "verbose": True,
    }
    response = api.post("/runs", json={"script_id": "mock-robot", "parameters": options})
    assert response.status_code == 201
    lines = []
    with api.websocket_connect("/events") as events:
        for _ in range(15):
            packet = events.receive_json()
            lines.extend(packet["logs"])
            if packet["run"]["exit_code"] is not None:
                break
        assert packet["run"]["exit_code"] == 0

    assert any("--tag for an experiment" in line["message"] for line in lines)
    assert any(line["level"] == "DEBUG" for line in lines)
    assert len([line for line in lines if line["event"] == "mock_tick"]) == 2
