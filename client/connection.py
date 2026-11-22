import asyncio
from collections.abc import AsyncIterator
from typing import Literal
from uuid import UUID

import httpx
from pydantic import TypeAdapter, ValidationError
from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus, WebSocketException

from common.parameters import Value
from common.protocol import (
    PROTOCOL_VERSION,
    EventFrame,
    Health,
    RunStarted,
    SafetyState,
    Script,
    valid_token,
)

CATALOG = TypeAdapter(list[Script])


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

    async def request(self, method: str, path: str, **kwargs: object) -> bytes:
        try:
            async with httpx.AsyncClient(
                base_url=self.url, headers=self.headers, trust_env=False, timeout=4
            ) as http:
                response = await http.request(method, path, **kwargs)
                response.raise_for_status()
        except httpx.RequestError as exc:
            raise RobotError("Агент недоступен.") from exc
        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code
            if code in (401, 403):
                raise RobotError("Агент отклонил токен.", retryable=False) from exc
            if path.startswith("/safety/"):
                detail = {
                    409: "Команда безопасности пока недоступна. Проверьте состояние робота.",
                    503: "Агент не подтвердил программное отключение двигателей.",
                }.get(code, f"Ошибка команды безопасности HTTP {code}.")
                raise RobotError(detail, retryable=code >= 500) from exc
            detail = {
                404: "Агент не нашёл скрипт или запуск.",
                409: "На агенте уже есть активный запуск.",
                422: "Агент отклонил параметры запуска.",
                503: "Агент не смог создать процесс.",
            }.get(code, f"Ошибка агента HTTP {code}.")
            if code == 409 and path == "/runs" and self.kind == "yahboom":
                detail = "Запуск недоступен. Проверьте состояние робота и аварийной блокировки."
            raise RobotError(detail, retryable=code >= 500) from exc
        return response.content

    async def catalog(self) -> list[Script]:
        try:
            health = Health.model_validate_json(await self.request("GET", "/health"))
            if health.protocol != PROTOCOL_VERSION:
                raise RobotError("Версия протокола агента отличается.", retryable=False)
            self.kind = health.robot
            return CATALOG.validate_json(await self.request("GET", "/scripts"))
        except ValidationError as exc:
            raise RobotError("Агент вернул неизвестный формат данных.", retryable=False) from exc

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
