import argparse
import asyncio
import errno
import hmac
import logging
import os
import stat
import sys
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from time import monotonic
from typing import Annotated, Literal
from uuid import UUID

import uvicorn
from fastapi import (
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    WebSocket,
    WebSocketDisconnect,
)
from starlette.requests import ClientDisconnect

from agent.camera import CameraBusy, CameraMissing, CameraPreview, CameraSource, CameraUnavailable
from agent.experiments import ExperimentStore, export_experiment
from agent.files import (
    DEFAULT_UPLOAD_BYTES,
    FileRoots,
    FileUpload,
    directory,
    file_io,
    parse_file_roots,
    valid_name,
)
from agent.supervisor import RunConflict, Supervisor
from agent.telemetry import TelemetrySampler
from agent.yahboom import DeviceBusy, Yahboom, load_yahboom
from common.protocol import (
    PROTOCOL_VERSION,
    CameraOptions,
    CameraStarted,
    Experiment,
    ExperimentDetails,
    FileReceipt,
    FileStorage,
    Health,
    Heartbeat,
    RunStarted,
    RunState,
    SafetyState,
    Script,
    StartRequest,
    SystemTelemetry,
    valid_token,
)

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
MOCK = Script.model_validate_json((EXAMPLES / "mock_robot.json").read_bytes())
REGISTRY = {MOCK.id: (MOCK, EXAMPLES / "mock_robot.py")}
MAX_TOKEN_FILE_BYTES = 256 + len(b"\r\n")
POWEROFF_HOLD_SECONDS = 30
logger = logging.getLogger(__name__)


def create_app(
    token: str,
    hardware: Yahboom | None = None,
    *,
    mock_camera: bool = False,
    data_dir: Path | None = None,
    file_roots: Mapping[str, Path] | None = None,
    max_upload_bytes: int = DEFAULT_UPLOAD_BYTES,
) -> FastAPI:
    if not valid_token(token):
        raise ValueError("RoboLab requires a URL-safe token of 32 to 256 characters")
    expected_auth = f"Bearer {token}".encode("ascii")
    history = ExperimentStore(data_dir / "history.sqlite3" if data_dir is not None else None)
    file_storage = FileRoots(file_roots, max_upload_bytes)
    sampler = TelemetrySampler()
    supervisor = Supervisor(hardware, history)
    poweroff_hold_until: float | None = None
    source = None
    if hardware is not None and hardware.settings.camera_device is not None:
        source = CameraSource(
            hardware.settings.python, hardware.settings.camera_device, hardware.acquire
        )
    elif mock_camera and hardware is None:
        source = CameraSource(sys.executable, "mock")
    camera = CameraPreview(source)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        try:
            if data_dir is not None:
                await asyncio.to_thread(data_dir.mkdir, parents=True, exist_ok=True)
            await history.open()
            yield
        finally:
            try:
                await camera.close()
            finally:
                try:
                    await supervisor.close()
                finally:
                    await history.close()

    def authorized(value: str | None) -> bool:
        return hmac.compare_digest((value or "").encode(), expected_auth)

    def require_auth(authorization: Annotated[str | None, Header()] = None) -> None:
        if not authorized(authorization):
            raise HTTPException(401, "Invalid token", headers={"WWW-Authenticate": "Bearer"})

    app = FastAPI(title="RoboLab Agent", lifespan=lifespan, docs_url=None, redoc_url=None)
    auth = [Depends(require_auth)]

    @app.get("/health", dependencies=auth, response_model_exclude_defaults=True)
    async def health() -> Health:
        return Health(
            protocol=PROTOCOL_VERSION,
            robot="yahboom" if hardware else "mock",
            camera_available=source is not None,
            observability_available=True,
            files_available=bool(file_storage.roots),
        )

    def upload_root(root: str, path: list[str]) -> Path:
        location = file_storage.roots.get(root)
        if location is None:
            raise HTTPException(404, "Папка назначения недоступна.")
        if any(not valid_name(part) for part in path):
            raise HTTPException(400, "Недопустимый путь папки.")
        return location

    @app.get("/files", dependencies=auth)
    async def files() -> FileStorage:
        if not file_storage.roots:
            raise HTTPException(404, "Хранилище файлов не настроено.")
        return FileStorage(
            roots=sorted(file_storage.roots), max_upload_bytes=file_storage.max_bytes
        )

    @app.get("/files/folders", dependencies=auth)
    async def folders(root: str, path: Annotated[list[str] | None, Query()] = None) -> list[str]:
        parts = path or []
        location = upload_root(root, parts)
        try:
            return await file_io(file_storage.folders, location, parts)
        except OSError as exc:
            raise HTTPException(404, "Папка назначения недоступна.") from exc

    @app.post("/files/upload", dependencies=auth, status_code=201)
    async def upload(
        request: Request, root: str, name: str, path: Annotated[list[str] | None, Query()] = None
    ) -> FileReceipt:
        parts = path or []
        location = upload_root(root, parts)
        if not valid_name(name):
            raise HTTPException(400, "Недопустимое имя файла.")
        if request.headers.get("content-type") != "application/octet-stream":
            raise HTTPException(415, "Ожидаются двоичные данные файла.")
        limit = file_storage.max_bytes
        declared = request.headers.get("content-length")
        try:
            expected = int(declared) if declared is not None else None
        except ValueError as exc:
            raise HTTPException(400, "Недопустимый размер файла.") from exc
        if expected is not None and expected < 0:
            raise HTTPException(400, "Недопустимый размер файла.")
        if expected is not None and expected > limit:
            raise HTTPException(413, "Размер файла превышает лимит агента.")
        transfer = FileUpload(location, parts, name)
        size = 0
        try:
            await file_io(transfer.open)
            async for chunk in request.stream():
                size += len(chunk)
                if size > limit:
                    raise HTTPException(413, "Размер файла превышает лимит агента.")
                if expected is not None and size > expected:
                    raise HTTPException(400, "Размер файла изменился во время передачи.")
                if chunk:
                    await file_io(transfer.write, chunk)
            if expected is not None and size != expected:
                raise HTTPException(400, "Файл передан не полностью.")
            await file_io(transfer.publish)
        except ClientDisconnect as exc:
            raise HTTPException(400, "Передача прервана.") from exc
        except FileExistsError as exc:
            raise HTTPException(409, "Файл с таким именем уже существует.") from exc
        except OSError as exc:
            if exc.errno in (errno.ENOSPC, errno.EDQUOT):
                raise HTTPException(507, "На роботе недостаточно места.") from exc
            if isinstance(exc, (FileNotFoundError, NotADirectoryError, PermissionError)):
                raise HTTPException(404, "Папка назначения недоступна.") from exc
            logger.exception("Could not write uploaded file")
            raise HTTPException(503, "Агент не смог сохранить файл.") from exc
        finally:
            try:
                await file_io(transfer.close)
            except OSError:
                logger.exception("Could not clear temporary upload")
        return FileReceipt(root=root, path=parts, name=name, size=size)

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
            configuration = script.configuration(request.parameters)
            arguments = script.arguments(configuration)
            if device is not None:
                hardware_seconds = float(configuration["max_seconds"])
                arguments = device.arguments(script_id, arguments)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        try:
            return await supervisor.start(
                script_id,
                path,
                arguments,
                hardware_seconds,
                script_name=script.name,
                parameters=configuration,
            )
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

    def camera_allowed() -> bool:
        return supervisor.safety is None or not supervisor.safety.latched

    @app.post("/camera", dependencies=auth, status_code=201)
    async def start_camera(options: CameraOptions) -> CameraStarted:
        try:
            return await camera.start(options, camera_allowed)
        except CameraMissing as exc:
            raise HTTPException(404, "Просмотр камеры не настроен.") from exc
        except (CameraBusy, DeviceBusy) as exc:
            raise HTTPException(409, "Камера или оборудование занято либо заблокировано.") from exc
        except OSError as exc:
            logger.exception("Could not start the camera worker")
            raise HTTPException(503, "Не удалось запустить просмотр камеры.") from exc

    @app.get("/camera/{camera_id}/frame", dependencies=auth)
    async def camera_frame(camera_id: UUID, after: Annotated[int, Query(ge=0)] = 0) -> Response:
        headers = {"Cache-Control": "no-store"}
        try:
            frame = await camera.frame(camera_id, after)
        except CameraMissing as exc:
            raise HTTPException(404, "Просмотр камеры завершён.") from exc
        except CameraUnavailable as exc:
            raise HTTPException(
                503, "Камера не передаёт кадры. Проверьте источник и OpenCV."
            ) from exc
        if frame is None:
            return Response(status_code=204, headers=headers)
        headers.update(
            {
                "X-Frame-Sequence": str(frame.sequence),
                "X-Frame-FPS": f"{frame.fps:.1f}",
                "X-Frame-Age-Ms": str(frame.age_ms),
            }
        )
        return Response(content=frame.jpeg, media_type="image/jpeg", headers=headers)

    @app.delete("/camera/{camera_id}", dependencies=auth, status_code=204)
    async def stop_camera(camera_id: UUID) -> None:
        try:
            await camera.stop(camera_id)
        except CameraMissing as exc:
            raise HTTPException(404, "Просмотр камеры завершён.") from exc
        except CameraUnavailable as exc:
            raise HTTPException(503, "Завершение процесса камеры не подтверждено.") from exc

    async def safety_action(reset: bool) -> SafetyState:
        if hardware is None:
            raise HTTPException(404, "Адаптер Yahboom не подключён.")
        try:
            if reset:
                if poweroff_hold_until is not None and monotonic() < poweroff_hold_until:
                    raise RunConflict(
                        "Сброс блокировки недоступен во время подготовки к выключению."
                    )
                return await supervisor.reset_safety()
            if hardware.settings.camera_device is not None:
                supervisor.inhibit("Запрошена аварийная остановка.")
                run = supervisor.active_run
                if run is not None and run.device_fd is not None:
                    supervisor.stop(run.id, force=True)
                await camera.stop()
            return await supervisor.emergency_stop()
        except RunConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except DeviceBusy as exc:
            raise HTTPException(409, "Оборудование занято другим процессом.") from exc
        except CameraUnavailable as exc:
            raise HTTPException(
                503, "Завершение камеры не подтверждено. Аппаратные запуски заблокированы."
            ) from exc
        except OSError as exc:
            logger.exception("Yahboom motor enable cutoff could not be confirmed")
            raise HTTPException(503, "Не удалось подтвердить низкий уровень на ENA и ENB.") from exc

    @app.post("/safety/stop", dependencies=auth)
    async def emergency_stop() -> SafetyState:
        return await safety_action(reset=False)

    @app.post("/safety/prepare-poweroff", dependencies=auth)
    async def prepare_poweroff() -> SafetyState:
        nonlocal poweroff_hold_until
        if hardware is not None:
            poweroff_hold_until = monotonic() + POWEROFF_HOLD_SECONDS
        return await safety_action(reset=False)

    @app.post("/safety/reset", dependencies=auth)
    async def reset_safety() -> SafetyState:
        return await safety_action(reset=True)

    @app.get("/telemetry", dependencies=auth)
    async def telemetry() -> SystemTelemetry:
        stats = await sampler.sample()
        run = supervisor.current
        active = supervisor.active_run
        preview = camera.current
        busy = bool(
            (active is not None and active.device_fd is not None)
            or (
                preview is not None
                and preview.lock_fd is not None
                and preview.task is not None
                and not preview.task.done()
            )
        )
        return SystemTelemetry(
            timestamp=stats.timestamp,
            cpu_percent=stats.cpu_percent,
            ram_used_bytes=stats.ram_used_bytes,
            ram_total_bytes=stats.ram_total_bytes,
            temperature_c=stats.temperature_c,
            network_rx_bytes=stats.network_rx_bytes,
            network_tx_bytes=stats.network_tx_bytes,
            network_operational=stats.network_operational,
            interfaces=stats.interfaces,
            robot="yahboom" if hardware is not None else "mock",
            camera_available=source is not None,
            hardware_busy=busy,
            run_id=run.id if run else None,
            metrics=run.metrics if run else {},
        )

    @app.get("/history", dependencies=auth)
    async def experiments(
        limit: Annotated[int, Query(ge=1, le=100)] = 25,
        offset: Annotated[int, Query(ge=0, le=1000000)] = 0,
    ) -> list[Experiment]:
        return await history.list(limit, offset)

    @app.get("/history/{run_id}", dependencies=auth)
    async def experiment(run_id: UUID) -> ExperimentDetails:
        details = await history.get(run_id)
        if details is None:
            raise HTTPException(404, "Запуск отсутствует в истории.")
        return details

    @app.get("/history/{run_id}/export", dependencies=auth)
    async def download_experiment(
        run_id: UUID, format: Literal["json", "csv"] = "json"
    ) -> Response:
        details = await experiment(run_id)
        body, media_type = export_experiment(details, format)
        return Response(
            content=body,
            media_type=media_type,
            headers={"Content-Disposition": f'attachment; filename="robolab-{run_id}.{format}"'},
        )

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


def read_token_file(path: Path) -> str:
    if sys.platform != "linux":
        raise ValueError("--token-file is supported on Linux. Set ROBOLAB_TOKEN instead.")
    try:
        path = path.expanduser().absolute()
        with directory(path.parts[1:-1]) as folder:
            fd = os.open(
                path.name,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                dir_fd=folder,
            )
            try:
                info = os.fstat(fd)
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_uid != os.geteuid()
                    or info.st_mode & 0o077
                ):
                    raise ValueError(
                        "--token-file requires a regular file owned by the agent user "
                        "with no group or other permissions."
                    )
                raw = os.read(fd, MAX_TOKEN_FILE_BYTES + 1)
            finally:
                os.close(fd)
    except (OSError, RuntimeError) as exc:
        raise ValueError("cannot read --token-file. Use a path without symbolic links.") from exc

    raw = raw[:-2] if raw.endswith(b"\r\n") else raw.removesuffix(b"\n")
    token = raw.decode("ascii", errors="replace")
    if not valid_token(token):
        raise ValueError("--token-file must contain a URL-safe token of 32 to 256 characters")
    return token


def run() -> None:
    parser = argparse.ArgumentParser(prog="robolab-agent")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument(
        "--token-file", type=Path, metavar="PATH", help="read a private token file on Linux"
    )
    parser.add_argument("--data-dir", type=Path, default=Path.home() / ".robolab" / "data")
    parser.add_argument("--file-root", action="append", default=[], metavar="ИМЯ=/путь")
    parser.add_argument("--max-upload-mib", type=int, default=DEFAULT_UPLOAD_BYTES // (1024**2))
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--yahboom-config", type=Path)
    source.add_argument("--mock-camera", action="store_true")
    options = parser.parse_args()
    if options.token_file is None:
        token = os.environ.get("ROBOLAB_TOKEN", "")
        if not valid_token(token):
            parser.error("set ROBOLAB_TOKEN to a URL-safe token of 32 to 256 characters")
    else:
        try:
            token = read_token_file(options.token_file)
        except ValueError as exc:
            parser.error(str(exc))
    try:
        hardware = load_yahboom(options.yahboom_config) if options.yahboom_config else None
        app = create_app(
            token,
            hardware,
            mock_camera=options.mock_camera,
            data_dir=options.data_dir,
            file_roots=parse_file_roots(options.file_root),
            max_upload_bytes=options.max_upload_mib * 1024**2,
        )
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    uvicorn.run(
        app,
        host=options.host,
        port=options.port,
        workers=1,
        ws="websockets-sansio",
        timeout_graceful_shutdown=5,
    )


if __name__ == "__main__":
    run()
