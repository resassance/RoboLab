import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from math import isfinite
from typing import Literal
from uuid import UUID

import httpx
from pydantic import TypeAdapter, ValidationError
from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus, WebSocketException

from common.parameters import Value
from common.protocol import (
    PROTOCOL_VERSION,
    CameraOptions,
    CameraStarted,
    EventFrame,
    Experiment,
    ExperimentDetails,
    FileReceipt,
    FileStorage,
    Health,
    RunStarted,
    SafetyState,
    Script,
    SystemTelemetry,
    valid_token,
)

CATALOG = TypeAdapter(list[Script])
HISTORY = TypeAdapter(list[Experiment])
FOLDERS = TypeAdapter(list[str])


@dataclass(frozen=True, slots=True)
class CameraFrame:
    jpeg: bytes
    sequence: int
    fps: float
    age_ms: float
    request_ms: float


class RobotError(Exception):
    def __init__(self, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.retryable = retryable


class RobotConnection:
    def __init__(self, address: str, token: str) -> None:
        address = address.strip()
        if "://" not in address:
            address = f"http://{address}"
        try:
            url = httpx.URL(address)
        except httpx.InvalidURL as exc:
            raise ValueError("Проверьте адрес агента.") from exc
        if (
            url.scheme not in ("http", "https")
            or not url.host
            or url.userinfo
            or url.path != "/"
            or url.query
            or url.fragment
        ):
            raise ValueError("Укажите адрес агента в виде http://хост:порт.")
        if not valid_token(token):
            raise ValueError("Введите токен агента из 32–256 символов.")
        self.url = url
        self.headers = {"Authorization": f"Bearer {token}"}
        self.kind: Literal["mock", "yahboom"] | None = None
        self.camera_available = False
        self.observability_available = False
        self.files_available = False

    def http_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self.url, headers=self.headers, trust_env=False, timeout=4
        )

    async def request(self, method: str, path: str, **kwargs: object) -> bytes:
        async with self.http_client() as http:
            return (await self.send(http, method, path, **kwargs)).content

    async def send(
        self, http: httpx.AsyncClient, method: str, path: str, **kwargs: object
    ) -> httpx.Response:
        try:
            response = await http.request(method, path, **kwargs)
            response.raise_for_status()
        except httpx.RequestError as exc:
            raise RobotError("Агент недоступен.") from exc
        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code
            if code in (401, 403):
                raise RobotError("Агент отклонил токен.", retryable=False) from exc
            if path == "/camera" or path.startswith("/camera/"):
                if code == 404 and method == "DELETE":
                    return exc.response
                detail = {
                    404: (
                        "Камера недоступна на агенте."
                        if path == "/camera"
                        else "Сессия камеры завершена. Запустите просмотр снова."
                    ),
                    409: "Камера или оборудование занято либо заблокировано.",
                    422: "Агент отклонил параметры камеры.",
                    503: {
                        "POST": "Агент не смог запустить камеру.",
                        "DELETE": "Агент не подтвердил завершение процесса камеры.",
                    }.get(method, "Агент не смог получить кадр с камеры."),
                }.get(code, f"Ошибка камеры HTTP {code}.")
                raise RobotError(detail, retryable=False) from exc
            if path.startswith("/safety/"):
                detail = {
                    409: "Команда безопасности пока недоступна. Проверьте состояние робота.",
                    503: "Агент не подтвердил программное отключение двигателей.",
                }.get(code, f"Ошибка команды безопасности HTTP {code}.")
                raise RobotError(detail, retryable=code >= 500) from exc
            if path == "/telemetry" or path == "/history" or path.startswith("/history/"):
                detail = {
                    404: (
                        "Запуск отсутствует в истории."
                        if path.startswith("/history/")
                        else "Агент не поддерживает этот раздел."
                    ),
                    422: "Агент отклонил параметры запроса.",
                }.get(code, f"Ошибка запроса HTTP {code}.")
                raise RobotError(detail, retryable=code >= 500) from exc
            if path == "/files" or path.startswith("/files/"):
                detail = {
                    400: "Проверьте имя файла и папку назначения.",
                    404: (
                        "Хранилище файлов недоступно на агенте."
                        if path == "/files"
                        else "Папка назначения недоступна на агенте."
                    ),
                    409: "В этой папке уже есть файл с таким именем.",
                    413: "Размер файла превышает лимит агента.",
                    422: "Агент отклонил параметры передачи файла.",
                    507: "На роботе недостаточно места для файла.",
                }.get(code, f"Ошибка передачи файлов HTTP {code}.")
                raise RobotError(detail, retryable=code >= 500 and code != 507) from exc
            detail = {
                404: "Агент не нашёл скрипт или запуск.",
                409: "На агенте уже есть активный запуск.",
                422: "Агент отклонил параметры запуска.",
                503: "Агент не смог создать процесс.",
            }.get(code, f"Ошибка агента HTTP {code}.")
            if code == 409 and path == "/runs" and self.kind == "yahboom":
                detail = "Запуск недоступен. Проверьте состояние робота и аварийной блокировки."
            raise RobotError(detail, retryable=code >= 500) from exc
        return response

    async def catalog(self) -> list[Script]:
        try:
            health = Health.model_validate_json(await self.request("GET", "/health"))
            if health.protocol != PROTOCOL_VERSION:
                raise RobotError("Версия протокола агента отличается.", retryable=False)
            self.kind = health.robot
            self.camera_available = health.camera_available
            self.observability_available = health.observability_available
            self.files_available = health.files_available
            return CATALOG.validate_json(await self.request("GET", "/scripts"))
        except ValidationError as exc:
            raise RobotError("Агент вернул неизвестный формат данных.", retryable=False) from exc

    async def telemetry(self, http: httpx.AsyncClient) -> SystemTelemetry:
        response = await self.send(http, "GET", "/telemetry")
        try:
            return SystemTelemetry.model_validate_json(response.content)
        except ValidationError as exc:
            raise RobotError(
                "Агент вернул неизвестный формат телеметрии.", retryable=False
            ) from exc

    async def history(self, *, limit: int, offset: int) -> list[Experiment]:
        data = await self.request("GET", "/history", params={"limit": limit, "offset": offset})
        try:
            return HISTORY.validate_json(data)
        except ValidationError as exc:
            raise RobotError("Агент вернул неизвестный формат истории.", retryable=False) from exc

    async def experiment(self, run_id: UUID) -> ExperimentDetails:
        data = await self.request("GET", f"/history/{run_id}")
        try:
            return ExperimentDetails.model_validate_json(data)
        except ValidationError as exc:
            raise RobotError(
                "Агент вернул неизвестный формат эксперимента.", retryable=False
            ) from exc

    async def export_experiment(self, run_id: UUID, format: Literal["csv", "json"]) -> bytes:
        return await self.request("GET", f"/history/{run_id}/export", params={"format": format})

    async def file_storage(self) -> FileStorage:
        data = await self.request("GET", "/files")
        try:
            return FileStorage.model_validate_json(data)
        except ValidationError as exc:
            raise RobotError("Агент вернул неизвестный формат хранилища.", retryable=False) from exc

    async def file_folders(self, root: str, path: tuple[str, ...]) -> list[str]:
        data = await self.request("GET", "/files/folders", params={"root": root, "path": path})
        try:
            return FOLDERS.validate_json(data)
        except ValidationError as exc:
            raise RobotError(
                "Агент вернул неизвестный формат списка папок.", retryable=False
            ) from exc

    async def upload_file(
        self, root: str, path: tuple[str, ...], name: str, source: AsyncIterator[bytes], size: int
    ) -> FileReceipt:
        data = await self.request(
            "POST",
            "/files/upload",
            params={"root": root, "path": path, "name": name},
            headers={"Content-Type": "application/octet-stream", "Content-Length": str(size)},
            content=source,
            timeout=httpx.Timeout(4.0, write=45.0, read=45.0),
        )
        try:
            return FileReceipt.model_validate_json(data)
        except ValidationError as exc:
            raise RobotError(
                "Агент вернул неизвестный формат ответа о передаче.", retryable=False
            ) from exc

    async def start_camera(self, options: CameraOptions) -> UUID:
        data = await self.request("POST", "/camera", json=options.model_dump(mode="json"))
        try:
            return CameraStarted.model_validate_json(data).id
        except ValidationError:
            raise RobotError("Агент вернул неизвестный формат сессии камеры.") from None

    async def camera_frame(
        self, http: httpx.AsyncClient, session_id: UUID, after: int
    ) -> CameraFrame | None:
        loop = asyncio.get_running_loop()
        started = loop.time()
        response = await self.send(
            http, "GET", f"/camera/{session_id}/frame", params={"after": after}
        )
        request_ms = (loop.time() - started) * 1000
        if response.status_code == 204:
            return None
        jpeg = response.content
        try:
            sequence = int(response.headers["X-Frame-Sequence"])
            fps = float(response.headers["X-Frame-FPS"])
            age_ms = float(response.headers["X-Frame-Age-Ms"])
            if (
                response.status_code == 200
                and response.headers.get("Content-Type", "").partition(";")[0] == "image/jpeg"
                and jpeg.startswith(b"\xff\xd8")
                and jpeg.endswith(b"\xff\xd9")
                and sequence > after
                and isfinite(fps)
                and isfinite(age_ms)
                and fps >= 0
                and age_ms >= 0
            ):
                return CameraFrame(jpeg, sequence, fps, age_ms, request_ms)
        except (KeyError, ValueError):
            pass
        raise RobotError("Агент вернул некорректный кадр камеры.", retryable=False)

    async def stop_camera(self, session_id: UUID) -> None:
        await self.request("DELETE", f"/camera/{session_id}")

    async def start(
        self, script_id: str, parameters: dict[str, Value], *, requires_heartbeat: bool = False
    ) -> RunStarted:
        payload: dict[str, object] = {"script_id": script_id}
        if parameters:
            payload["parameters"] = parameters
        if requires_heartbeat:
            payload["supports_lease"] = True
        data = await self.request("POST", "/runs", json=payload)
        try:
            started = RunStarted.model_validate_json(data)
        except ValidationError:
            raise RobotError("Агент вернул неизвестный формат запуска.", retryable=False) from None
        if requires_heartbeat and started.status == "running" and started.lease is None:
            raise RobotError("Агент не выдал подтверждение удержания запуска.", retryable=False)
        return started

    async def heartbeat(self, run_id: UUID, lease: UUID) -> None:
        await self.request("POST", f"/runs/{run_id}/heartbeat", json={"lease": str(lease)})

    async def stop(self, run_id: UUID) -> None:
        await self.request("POST", f"/runs/{run_id}/stop")

    async def safety_stop(self) -> SafetyState:
        return await self.safety_request("stop")

    async def safety_reset(self) -> SafetyState:
        return await self.safety_request("reset")

    async def safety_request(self, action: Literal["stop", "reset"]) -> SafetyState:
        data = await self.request("POST", f"/safety/{action}")
        try:
            return SafetyState.model_validate_json(data)
        except ValidationError as exc:
            raise RobotError("Агент вернул неизвестное состояние безопасности.") from exc

    async def events(self, run_id: UUID | None, after: int) -> AsyncIterator[EventFrame]:
        url = self.url.copy_with(
            scheme="wss" if self.url.scheme == "https" else "ws",
            path="/events",
            params={"run_id": str(run_id), "after": after} if run_id else None,
        )
        try:
            async with connect(
                str(url),
                additional_headers=self.headers,
                proxy=None,
                open_timeout=4,
                close_timeout=1,
                max_size=2**22,
            ) as socket:
                while True:
                    data = await asyncio.wait_for(socket.recv(), timeout=5)
                    yield EventFrame.model_validate_json(data)
        except InvalidStatus as exc:
            if exc.response.status_code in (401, 403):
                raise RobotError("Агент отклонил токен.", retryable=False) from exc
            raise RobotError("Агент не открыл поток событий.") from exc
        except (OSError, TimeoutError, WebSocketException) as exc:
            raise RobotError("Связь с агентом прервана.") from exc
        except ValidationError as exc:
            raise RobotError("Неизвестный формат событий агента.", retryable=False) from exc
