import asyncio
from collections.abc import AsyncIterator
from uuid import UUID

import httpx
from pydantic import TypeAdapter, ValidationError
from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus, WebSocketException

from common.protocol import PROTOCOL_VERSION, EventFrame, Health, Script, valid_token

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
            detail = {
                404: "Агент не нашёл скрипт или запуск.",
                409: "На агенте уже есть активный запуск.",
                503: "Агент не смог создать процесс.",
            }.get(code, f"Ошибка агента HTTP {code}.")
            raise RobotError(detail, retryable=code >= 500) from exc
        return response.content

    async def catalog(self) -> list[Script]:
        try:
            health = Health.model_validate_json(await self.request("GET", "/health"))
            if health.protocol != PROTOCOL_VERSION:
                raise RobotError("Версия протокола агента отличается.", retryable=False)
            return CATALOG.validate_json(await self.request("GET", "/scripts"))
        except ValidationError as exc:
            raise RobotError("Агент вернул неизвестный формат данных.", retryable=False) from exc

    async def start(self, script_id: str) -> None:
        await self.request("POST", "/runs", json={"script_id": script_id})

    async def stop(self, run_id: UUID) -> None:
        await self.request("POST", f"/runs/{run_id}/stop")

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
