import asyncio
from contextlib import asynccontextmanager
from uuid import uuid4

import httpx

from agent import main, supervisor
from agent.main import create_app

TOKEN = "local-manual-test-token-at-least-32-characters"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}


@asynccontextmanager
async def client_session():
    app = create_app(TOKEN)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test", headers=HEADERS
        ) as client:
            yield client


def test_manual_is_authenticated_exclusive_and_lease_bound():
    async def check():
        async with client_session() as client:
            assert (await client.get("/health")).json()["manual_available"] is True
            assert (
                await client.post(
                    "/manual", json={"direction": "forward"}, headers={"Authorization": ""}
                )
            ).status_code == 401
            assert (await client.post("/manual", json={"direction": "anything"})).status_code == 422
            assert (
                await client.post("/manual", json={"direction": "forward", "seconds": 20})
            ).status_code == 422
            response = await client.post("/manual", json={"direction": "forward"})
            assert response.status_code == 201
            run = response.json()
            stop = f"/manual/{run['id']}/stop"
            assert (await client.post("/manual", json={"direction": "right"})).status_code == 409
            assert (await client.post("/runs", json={"script_id": "mock-robot"})).status_code == 409
            assert (await client.post(stop, json={"lease": str(uuid4())})).status_code == 409
            assert (await client.post(stop, json={"lease": run["lease"]})).status_code == 204
            assert (await client.post("/manual", json={"direction": "right"})).status_code == 201
            assert (await client.post(stop, json={"lease": run["lease"]})).status_code == 204
            assert (await client.post("/manual", json={"direction": "left"})).status_code == 409

    asyncio.run(check())


def test_manual_lease_times_out_and_heartbeat_does_not_extend_hard_limit(monkeypatch):
    monkeypatch.setattr(supervisor, "MANUAL_HEARTBEAT_TIMEOUT", 0.25)
    monkeypatch.setattr(main, "MANUAL_LIMIT", 0.65)

    async def check():
        async with client_session() as client:
            run = (await client.post("/manual", json={"direction": "backward"})).json()
            path = f"/runs/{run['id']}/heartbeat"
            lease = {"lease": run["lease"]}
            assert (await client.post(path, json=lease)).status_code == 204
            await asyncio.sleep(0.33)
            assert (await client.post(path, json=lease)).status_code == 409

            run = (await client.post("/manual", json={"direction": "forward"})).json()
            heartbeat = f"/runs/{run['id']}/heartbeat"
            lease = {"lease": run["lease"]}
            for _ in range(3):
                assert (await client.post(heartbeat, json=lease)).status_code == 204
                await asyncio.sleep(0.13)
            assert (await client.post(heartbeat, json=lease)).status_code == 204
            await asyncio.sleep(0.29)
            assert (await client.post(heartbeat, json=lease)).status_code == 409

    asyncio.run(check())
