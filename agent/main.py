import argparse
import asyncio
import hmac
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated
from uuid import UUID

import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException, Query, WebSocket, WebSocketDisconnect

from agent.supervisor import RunConflict, Supervisor
from common.protocol import PROTOCOL_VERSION, Health, RunState, Script, StartRequest, valid_token

MOCK = Script(
    id="mock-robot",
    name="Mock Robot",
    description="Демонстрационный алгоритм. Пишет события и шаги симуляции без доступа к роботу.",
)
REGISTRY = {MOCK.id: (MOCK, Path(__file__).resolve().parent.parent / "examples" / "mock_robot.py")}
logger = logging.getLogger(__name__)


def create_app(token: str) -> FastAPI:
    if not valid_token(token):
        raise ValueError("RoboLab requires a URL-safe token of 32 to 256 characters")
    expected_auth = f"Bearer {token}".encode("ascii")
    supervisor = Supervisor()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        yield
        await supervisor.close()

    def authorized(value: str | None) -> bool:
        return hmac.compare_digest((value or "").encode(), expected_auth)

    def require_auth(authorization: Annotated[str | None, Header()] = None) -> None:
        if not authorized(authorization):
            raise HTTPException(401, "Invalid token", headers={"WWW-Authenticate": "Bearer"})

    app = FastAPI(title="RoboLab Agent", lifespan=lifespan, docs_url=None, redoc_url=None)
    auth = [Depends(require_auth)]

    @app.get("/health", dependencies=auth)
    async def health() -> Health:
        return Health(protocol=PROTOCOL_VERSION, robot="mock")

    @app.get("/scripts", dependencies=auth)
    async def scripts() -> list[Script]:
        return [script for script, _ in REGISTRY.values()]

    @app.post("/runs", dependencies=auth, status_code=201)
    async def start(request: StartRequest) -> RunState:
        registration = REGISTRY.get(request.script_id)
        if registration is None:
            raise HTTPException(404, "Unknown script")
        _, path = registration
        try:
            return await supervisor.start(request.script_id, path)
        except RunConflict as exc:
            raise HTTPException(409, "A run already occupies the agent") from exc
        except OSError as exc:
            logger.exception("Could not launch registered script %s", request.script_id)
            raise HTTPException(503, "Could not start the registered script") from exc

    @app.post("/runs/{run_id}/stop", dependencies=auth)
    async def stop(run_id: UUID) -> RunState:
        try:
            return supervisor.stop(run_id)
        except KeyError as exc:
            raise HTTPException(404, "Unknown run") from exc

    @app.websocket("/events")
    async def events(
        socket: WebSocket,
        run_id: UUID | None = None,
        after: Annotated[int, Query(ge=0)] = 0,
    ) -> None:
        if not authorized(socket.headers.get("authorization")):
            await socket.close(code=1008)
            return
        await socket.accept()
        try:
            while True:
                changed = supervisor.changed
                packet = supervisor.frame(run_id, after)
                await socket.send_json(packet.model_dump(mode="json"))
                if packet.run and packet.run.id != run_id:
                    run_id, after = packet.run.id, 0
                if packet.logs:
                    after = packet.logs[-1].sequence
                try:
                    await asyncio.wait_for(changed.wait(), timeout=1)
                except TimeoutError:
                    pass
        except (WebSocketDisconnect, OSError):
            pass

    return app


def run() -> None:
    parser = argparse.ArgumentParser(prog="robolab-agent")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    options = parser.parse_args()
    token = os.environ.get("ROBOLAB_TOKEN", "")
    if not valid_token(token):
        parser.error("set ROBOLAB_TOKEN to a URL-safe token of 32 to 256 characters")
    uvicorn.run(
        create_app(token), host=options.host, port=options.port, workers=1, ws="websockets-sansio"
    )


if __name__ == "__main__":
    run()
