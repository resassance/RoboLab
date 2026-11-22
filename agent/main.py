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
from agent.yahboom import DeviceBusy, Yahboom, load_yahboom
from common.protocol import (
    PROTOCOL_VERSION,
    Health,
    Heartbeat,
    RunStarted,
    RunState,
    SafetyState,
    Script,
    StartRequest,
    valid_token,
)

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
MOCK = Script.model_validate_json((EXAMPLES / "mock_robot.json").read_bytes())
REGISTRY = {MOCK.id: (MOCK, EXAMPLES / "mock_robot.py")}
logger = logging.getLogger(__name__)


def create_app(token: str, hardware: Yahboom | None = None) -> FastAPI:
    if not valid_token(token):
        raise ValueError("RoboLab requires a URL-safe token of 32 to 256 characters")
    expected_auth = f"Bearer {token}".encode("ascii")
    supervisor = Supervisor(hardware)

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
        return Health(protocol=PROTOCOL_VERSION, robot="yahboom" if hardware else "mock")

    @app.get("/scripts", dependencies=auth)
    async def scripts() -> list[Script]:
        scripts = list(hardware.catalog.values()) if hardware is not None else []
        scripts.extend(script for script, _ in REGISTRY.values())
        return scripts

    @app.post("/runs", dependencies=auth, status_code=201)
    async def start(request: StartRequest) -> RunStarted:
        script_id = request.script_id
        device = hardware if hardware is not None and script_id in hardware.catalog else None
        registration = (
            (device.catalog[script_id], device.script_path) if device else REGISTRY.get(script_id)
        )
        if registration is None:
            raise HTTPException(404, "Unknown script")
        if device is not None and not request.supports_lease:
            raise HTTPException(422, "Для запуска Yahboom нужен клиент с контролем связи.")
        script, path = registration
        hardware_seconds = None
        try:
            arguments = script.arguments(request.parameters)
            if device is not None:
                configuration = script.configuration(request.parameters)
                hardware_seconds = float(configuration["max_seconds"])
                arguments = device.arguments(script_id, arguments)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        try:
            return await supervisor.start(script_id, path, arguments, hardware_seconds)
        except RunConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except DeviceBusy as exc:
            raise HTTPException(409, "Оборудование занято другим процессом.") from exc
        except OSError as exc:
            logger.exception("Could not launch registered script %s", script_id)
            raise HTTPException(503, "Агент не смог подготовить или создать процесс.") from exc

    @app.post("/runs/{run_id}/heartbeat", dependencies=auth, status_code=204)
    async def heartbeat(run_id: UUID, request: Heartbeat) -> None:
        try:
            supervisor.heartbeat(run_id, request.lease)
        except RunConflict as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/runs/{run_id}/stop", dependencies=auth)
    async def stop(run_id: UUID) -> RunState:
        try:
            return supervisor.stop(run_id)
        except KeyError as exc:
            raise HTTPException(404, "Unknown run") from exc

    async def safety_action(reset: bool) -> SafetyState:
        if hardware is None:
            raise HTTPException(404, "Адаптер Yahboom не подключён.")
        try:
            if reset:
                return await supervisor.reset_safety()
            return await supervisor.emergency_stop()
        except RunConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except DeviceBusy as exc:
            raise HTTPException(409, "Оборудование занято другим процессом.") from exc
        except OSError as exc:
            logger.exception("Yahboom motor enable cutoff could not be confirmed")
            raise HTTPException(503, "Не удалось подтвердить низкий уровень на ENA и ENB.") from exc

    @app.post("/safety/stop", dependencies=auth)
    async def emergency_stop() -> SafetyState:
        return await safety_action(reset=False)

    @app.post("/safety/reset", dependencies=auth)
    async def reset_safety() -> SafetyState:
        return await safety_action(reset=True)

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
    parser.add_argument("--yahboom-config", type=Path)
    options = parser.parse_args()
    token = os.environ.get("ROBOLAB_TOKEN", "")
    if not valid_token(token):
        parser.error("set ROBOLAB_TOKEN to a URL-safe token of 32 to 256 characters")
    try:
        hardware = load_yahboom(options.yahboom_config) if options.yahboom_config else None
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    uvicorn.run(
        create_app(token, hardware),
        host=options.host,
        port=options.port,
        workers=1,
        ws="websockets-sansio",
        timeout_graceful_shutdown=5,
    )


if __name__ == "__main__":
    run()
