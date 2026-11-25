import asyncio
import posixpath
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from uuid import uuid4

import asyncssh

from client.connection import RobotError
from common.file_paths import DEFAULT_UPLOAD_BYTES, UPLOAD_PREFIX, valid_name
from common.protocol import FileReceipt, FileStorage

HOME_ROOT = "Домашняя папка"
MISSING = (asyncssh.SFTPNoSuchFile, asyncssh.SFTPNoSuchPath)


class SftpFiles:
    def __init__(self, connection: asyncssh.SSHClientConnection) -> None:
        self.connection = connection

    @asynccontextmanager
    async def open(self, timeout: int, message: str) -> AsyncIterator[asyncssh.SFTPClient]:
        try:
            async with asyncio.timeout(timeout), self.connection.start_sftp_client() as sftp:
                yield sftp
        except (asyncssh.Error, TimeoutError) as exc:
            raise RobotError(message) from exc

    @staticmethod
    async def folder(sftp: asyncssh.SFTPClient, root: str, path: tuple[str, ...]) -> bytes:
        if root != HOME_ROOT or any(not valid_name(part) for part in path):
            raise RobotError("Недопустимый путь папки.", retryable=False)
        home = await sftp.realpath(b".")
        selected = posixpath.join(home, *(part.encode("utf-8") for part in path))
        if (
            not posixpath.isabs(home)
            or await sftp.realpath(selected) != selected
            or (await sftp.lstat(selected)).type != asyncssh.FILEXFER_TYPE_DIRECTORY
        ):
            raise RobotError("Папка назначения недоступна.", retryable=False)
        return selected

    async def file_storage(self) -> FileStorage:
        async with self.open(10, "Не удалось открыть домашнюю папку по SFTP.") as sftp:
            await self.folder(sftp, HOME_ROOT, ())
        return FileStorage(roots=[HOME_ROOT], max_upload_bytes=DEFAULT_UPLOAD_BYTES)

    async def file_folders(self, root: str, path: tuple[str, ...]) -> list[str]:
        async with self.open(15, "Не удалось получить список папок по SFTP.") as sftp:
            folder = await self.folder(sftp, root, path)
            names = []
            async for entry in sftp.scandir(folder):
                if entry.attrs.type == asyncssh.FILEXFER_TYPE_DIRECTORY:
                    name = entry.filename.decode("utf-8", errors="surrogateescape")
                    if valid_name(name):
                        names.append(name)
        return sorted(names, key=lambda name: (name.casefold(), name))

    async def create_folder(
        self, root: str, path: tuple[str, ...], name: str, *, exist_ok: bool = False
    ) -> None:
        if not valid_name(name):
            raise RobotError("Недопустимое имя папки.", retryable=False)
        async with self.open(15, "Не удалось создать папку по SFTP.") as sftp:
            folder = await self.folder(sftp, root, path)
            destination = posixpath.join(folder, name.encode("utf-8"))
            try:
                await sftp.mkdir(destination, asyncssh.SFTPAttrs(permissions=0o700))
            except asyncssh.SFTPError as exc:
                try:
                    existing = await sftp.lstat(destination)
                except MISSING:
                    raise exc from None
                if exist_ok and existing.type == asyncssh.FILEXFER_TYPE_DIRECTORY:
                    await self.folder(sftp, root, (*path, name))
                    return
                raise RobotError(
                    "Здесь уже есть файл или папка с таким именем.", retryable=False
                ) from exc

    @staticmethod
    async def check_destination(sftp: asyncssh.SFTPClient, file: bytes) -> None:
        try:
            await sftp.lstat(file)
        except MISSING:
            return
        raise RobotError("Файл с таким именем уже существует.", retryable=False)

    async def upload_file(
        self, root: str, path: tuple[str, ...], name: str, source: AsyncIterator[bytes], size: int
    ) -> FileReceipt:
        if not valid_name(name):
            raise RobotError("Недопустимое имя файла.", retryable=False)
        if not 0 <= size <= DEFAULT_UPLOAD_BYTES:
            raise RobotError(
                "Размер файла превышает лимит передачи или недопустим.", retryable=False
            )

        async with self.open(120, "Не удалось сохранить файл на роботе по SFTP.") as sftp:
            folder = await self.folder(sftp, root, path)
            destination = posixpath.join(folder, name.encode("utf-8"))
            await self.check_destination(sftp, destination)
            staging = posixpath.join(folder, f"{UPLOAD_PREFIX}{uuid4().hex}".encode("ascii"))
            transferred = 0
            try:
                async with sftp.open(staging, "xb", asyncssh.SFTPAttrs(permissions=0o600)) as file:
                    async for chunk in source:
                        if transferred + len(chunk) > size:
                            raise RobotError("Размер файла изменился во время передачи.")
                        if chunk:
                            transferred += await file.write(chunk)
                if transferred != size:
                    raise RobotError("Файл передан не полностью.")
                if await self.folder(sftp, root, path) != folder:
                    raise RobotError("Папка назначения изменилась во время передачи.")
                try:
                    await sftp.link(staging, destination)
                except asyncssh.SFTPOpUnsupported as exc:
                    raise RobotError(
                        "Сервер SFTP не поддерживает безопасное сохранение файла."
                    ) from exc
                except asyncssh.SFTPError:
                    await self.check_destination(sftp, destination)
                    raise
            finally:
                with suppress(asyncssh.Error, TimeoutError):
                    async with asyncio.timeout(5):
                        if await sftp.realpath(folder) == folder:
                            await sftp.remove(staging)
        return FileReceipt(root=root, path=list(path), name=name, size=transferred)
