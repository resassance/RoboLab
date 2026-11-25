import asyncio
import os
import stat
import sys
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import TypeVar
from uuid import uuid4

from common.file_paths import DEFAULT_UPLOAD_BYTES as DEFAULT_UPLOAD_BYTES
from common.file_paths import UPLOAD_PREFIX, valid_name

T = TypeVar("T")


@contextmanager
def directory(parts: tuple[str, ...]):
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = os.open("/", flags)
    try:
        for part in parts:
            child = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


async def file_io(action: Callable[..., T], *args: object) -> T:
    task = asyncio.create_task(asyncio.to_thread(action, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not task.cancelled():
            task.exception()
        raise


class FileRoots:
    def __init__(self, roots: Mapping[str, Path] | None, max_bytes: int) -> None:
        if max_bytes <= 0:
            raise ValueError("Лимит передачи должен быть положительным.")
        if roots and sys.platform != "linux":
            raise ValueError("Приём файлов агентом поддерживается на Linux.")
        self.roots = dict(roots or {})
        self.max_bytes = max_bytes
        for name, root in self.roots.items():
            if not valid_name(name) or not root.is_absolute() or ".." in root.parts:
                raise ValueError("Для папки нужен допустимый ярлык и абсолютный путь.")
            try:
                with directory(root.parts[1:]):
                    pass
            except (OSError, ValueError) as exc:
                raise ValueError(
                    f"Папка «{name}» недоступна или содержит символическую ссылку."
                ) from exc

    def folders(self, root: Path, path: list[str]) -> list[str]:
        with directory((*root.parts[1:], *path)) as fd, os.scandir(fd) as entries:
            return sorted(
                (
                    entry.name
                    for entry in entries
                    if valid_name(entry.name) and entry.is_dir(follow_symlinks=False)
                ),
                key=lambda name: (name.casefold(), name),
            )

    def create_folder(self, root: Path, path: list[str], name: str, exist_ok: bool) -> None:
        with directory((*root.parts[1:], *path)) as fd:
            try:
                os.mkdir(name, mode=0o700, dir_fd=fd)
            except FileExistsError:
                if not exist_ok or not stat.S_ISDIR(
                    os.stat(name, dir_fd=fd, follow_symlinks=False).st_mode
                ):
                    raise


class FileUpload:
    def __init__(self, root: Path, path: list[str], name: str) -> None:
        self.location = directory((*root.parts[1:], *path))
        self.name = name
        self.staging = f"{UPLOAD_PREFIX}{uuid4().hex}"
        self.folder: int | None = None
        self.fd: int | None = None

    def open(self) -> None:
        self.folder = self.location.__enter__()
        try:
            os.stat(self.name, dir_fd=self.folder, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise FileExistsError(self.name)
        self.fd = os.open(
            self.staging,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=self.folder,
        )

    def write(self, chunk: bytes) -> None:
        assert self.fd is not None
        pending = memoryview(chunk)
        while pending:
            pending = pending[os.write(self.fd, pending) :]

    def publish(self) -> None:
        assert self.fd is not None and self.folder is not None
        os.fsync(self.fd)
        os.link(
            self.staging,
            self.name,
            src_dir_fd=self.folder,
            dst_dir_fd=self.folder,
            follow_symlinks=False,
        )

    def close(self) -> None:
        if self.folder is None:
            return
        try:
            if self.fd is not None:
                try:
                    os.close(self.fd)
                finally:
                    os.unlink(self.staging, dir_fd=self.folder)
        finally:
            self.location.__exit__(None, None, None)


def parse_file_roots(values: list[str]) -> dict[str, Path]:
    roots: dict[str, Path] = {}
    for value in values:
        name, separator, path = value.partition("=")
        if not separator or not path or name in roots:
            raise ValueError("--file-root требует уникальное имя и путь в формате ИМЯ=/путь.")
        roots[name] = Path(path)
    return roots
