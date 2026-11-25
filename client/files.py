from __future__ import annotations

import asyncio
import os
import stat
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import aclosing, suppress
from pathlib import Path
from time import monotonic
from typing import TYPE_CHECKING
from uuid import UUID

import flet as ft

from client.appearance import (
    AMBER,
    AMBER_TINT,
    CONTROL_RADIUS,
    ERROR,
    ERROR_TINT,
    LINE,
    MUTED,
    SUCCESS,
    SUCCESS_TINT,
    SURFACE,
    TEAL,
    TEAL_TINT,
    TEXT,
    button_row,
)
from client.connection import RobotConnection, RobotError
from client.sftp_files import SftpFiles
from common.file_paths import valid_name
from common.protocol import FileReceipt, FileStorage

if TYPE_CHECKING:
    from robolab_dropzone import DropEvent

try:
    from robolab_dropzone import DropZone
except ImportError:
    DropZone = None

BLOCK = 128 * 1024


def file_size(size: int) -> str:
    if size < 1024:
        return f"{size} Б"
    if size < 1024**2:
        return f"{size / 1024:.0f} КБ"
    return f"{size / 1024**2:.1f} МБ"


def open_file(path: str, expected_size: int, identity: tuple[int, int] | None = None) -> int:
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
    if identity is not None:
        flags |= getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_size != expected_size
            or (identity is not None and (info.st_dev, info.st_ino) != identity)
        ):
            raise OSError("The selected file changed.")
    except OSError:
        os.close(fd)
        raise
    return fd


async def file_chunks(
    file: ft.FilePickerFile, identity: tuple[int, int] | None = None
) -> AsyncIterator[bytes]:
    if file.bytes is not None:
        if len(file.bytes) != file.size:
            raise OSError("The selected file changed.")
        for start in range(0, file.size, BLOCK):
            yield file.bytes[start : start + BLOCK]
        return
    if file.path is None:
        raise OSError("The selected file is unavailable.")
    fd = open_file(file.path, file.size, identity)
    try:
        remaining = file.size
        while remaining:
            read = asyncio.create_task(asyncio.to_thread(os.read, fd, min(BLOCK, remaining)))
            try:
                chunk = await asyncio.shield(read)
            except asyncio.CancelledError:
                while not read.done():
                    try:
                        await asyncio.shield(read)
                    except asyncio.CancelledError:
                        continue
                    except OSError:
                        break
                if not read.cancelled():
                    read.exception()
                raise
            if not chunk:
                raise OSError("The selected file changed.")
            remaining -= len(chunk)
            yield chunk
    finally:
        os.close(fd)


def drop_entries(source: Path, max_bytes: int) -> Iterator[tuple[tuple[str, ...], os.stat_result]]:
    pending = [(source, ())]
    while pending:
        local, parent = pending.pop()
        name = local.name
        if not valid_name(name):
            raise RobotError("Недопустимое имя файла или папки.", retryable=False)
        info = local.lstat()
        destination = (*parent, name)
        if getattr(info, "st_reparse_tag", None) == getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", -1):
            raise RobotError("Нельзя отправить ссылку на папку.", retryable=False)
        if stat.S_ISDIR(info.st_mode):
            pending.extend((child, destination) for child in sorted(local.iterdir(), reverse=True))
        elif not stat.S_ISREG(info.st_mode):
            raise RobotError(f"«{name}» — ссылка или специальный файл.", retryable=False)
        elif info.st_size > max_bytes:
            raise RobotError(f"Файл «{name}» превышает лимит {file_size(max_bytes)}.")
        yield destination, info


class FilePanel:
    def __init__(
        self,
        page: ft.Page,
        robot: RobotConnection | SftpFiles,
        picker: ft.FilePicker,
        back: Callable[[ft.ControlEvent], Awaitable[None]],
        compact: bool,
    ) -> None:
        self.page = page
        self.robot = robot
        self.picker = picker
        self.storage: FileStorage | None = None
        self.file: ft.FilePickerFile | None = None
        self.root: str | None = None
        self.path: tuple[str, ...] = ()
        self.folders: list[str] = []
        self.task: asyncio.Task[None] | None = None
        self.closed = False
        self.picking = False
        self.creating_folder = False
        self.sending = False
        self.sent = 0
        self.compact = compact

        self.back = ft.IconButton(ft.Icons.ARROW_BACK, tooltip="К работе", on_click=back)
        self.reload = ft.IconButton(
            ft.Icons.REFRESH, tooltip="Обновить папки", on_click=self.reload_folders
        )
        self.heading = ft.Text("Файлы", size=34, weight=ft.FontWeight.W_700, expand=True)
        self.breadcrumb = ft.Row(spacing=4, scroll=ft.ScrollMode.AUTO, auto_scroll=True)
        self.folders_view = ft.ListView(expand=True, spacing=0)
        self.pick = ft.FilledButton(
            "Выбрать файл", icon=ft.Icons.UPLOAD_FILE_OUTLINED, height=44, on_click=self.pick_file
        )
        self.replace = ft.OutlinedButton(
            "Другой файл", icon=ft.Icons.UPLOAD_FILE_OUTLINED, height=44, on_click=self.pick_file
        )
        self.filename = ft.Text(expand=True, max_lines=1, overflow=ft.TextOverflow.ELLIPSIS)
        self.destination = ft.Text(color=MUTED, max_lines=2, overflow=ft.TextOverflow.ELLIPSIS)
        self.send = ft.FilledButton("Отправить", height=44, on_click=self.start_upload)
        self.cancel = ft.OutlinedButton("Отменить отправку", height=44, on_click=self.cancel_upload)
        self.progress = ft.ProgressBar(expand=True, value=0, color=AMBER, bar_height=5)
        self.progress_label = ft.Text(size=13, color=AMBER)
        self.notice = ft.Text(size=14, selectable=True, visible=False)
        self.feedback = ft.Container(
            self.notice,
            padding=ft.Padding.symmetric(vertical=10, horizontal=12),
            border_radius=CONTROL_RADIUS,
            visible=False,
        )
        self.summary = ft.ResponsiveRow(
            [
                ft.Row(
                    [ft.Icon(ft.Icons.DESCRIPTION_OUTLINED, color=TEAL, size=20), self.filename],
                    col={"xs": 12, "md": 6},
                ),
                ft.Container(self.destination, col={"xs": 12, "md": 6}),
            ],
            spacing=10,
            run_spacing=8,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )
        self.selection = ft.Container(
            self.summary,
            bgcolor=TEAL_TINT,
            border_radius=CONTROL_RADIUS,
            padding=12,
        )
        self.actions = button_row(self.pick, self.replace, self.send, self.cancel)
        self.progress_row = ft.Container(
            ft.Row([self.progress, self.progress_label], spacing=10),
            bgcolor=AMBER_TINT,
            border_radius=CONTROL_RADIUS,
            padding=12,
        )
        self.new_folder = ft.OutlinedButton(
            "Новая папка",
            icon=ft.Icons.CREATE_NEW_FOLDER_OUTLINED,
            height=44,
            on_click=self.show_new_folder,
        )
        self.native_drop = (
            DropZone is not None
            and not page.web
            and page.platform in (ft.PagePlatform.LINUX, ft.PagePlatform.WINDOWS)
        )
        self.drop_hint = ft.Text(
            "Перетащите файлы или папки сюда", color=MUTED, size=13, visible=False
        )
        self.tray = ft.Container(
            border=ft.Border.only(top=ft.BorderSide(1, LINE)),
            padding=ft.Padding.only(top=16, bottom=4),
            content=ft.Column(
                [self.drop_hint, self.selection, self.progress_row, self.feedback, self.actions],
                spacing=12,
                horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
            ),
        )
        layout = ft.Column(
            [
                ft.Row([self.back, self.heading, self.reload], spacing=4),
                ft.Container(
                    bgcolor=TEAL_TINT,
                    border_radius=CONTROL_RADIUS,
                    padding=ft.Padding.symmetric(horizontal=6, vertical=2),
                    content=self.breadcrumb,
                ),
                self.new_folder,
                self.folders_view,
                self.tray,
            ],
            spacing=12,
            horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
        )
        self.drop_zone = (
            DropZone(content=layout, on_drop=self.receive_drop) if self.native_drop else None
        )
        self.content = ft.Container(expand=True, content=self.drop_zone or layout)
        self.render()

    def set_compact(self, compact: bool) -> None:
        self.compact = compact
        self.back.visible = compact
        self.heading.size = 28 if compact else 34
        self.content.padding = ft.Padding.symmetric(
            horizontal=16 if compact else 26, vertical=14 if compact else 22
        )

    def start(self) -> None:
        self.request(self.load_storage)

    def close(self) -> asyncio.Task[None] | None:
        self.closed = True
        self.clear_file()
        if self.task is not None:
            self.task.cancel()
        return self.task

    def request(self, action: Callable[[], Awaitable[None]]) -> None:
        if self.closed or self.task is not None or self.picking:
            return
        self.message("")
        self.task = asyncio.create_task(self.perform(action))
        self.refresh()

    async def perform(self, action: Callable[[], Awaitable[None]]) -> None:
        try:
            await action()
        except RobotError as exc:
            self.message(str(exc), error=True)
        finally:
            self.task = None
            self.sending = False
            self.refresh()

    async def load_storage(self) -> None:
        self.storage = await self.robot.file_storage()
        if len(self.storage.roots) == 1:
            await self.load_folders(self.storage.roots[0], ())

    async def load_folders(self, root: str | None, path: tuple[str, ...]) -> None:
        folders = await self.robot.file_folders(root, path) if root is not None else []
        self.root, self.path, self.folders = root, path, folders

    async def reload_folders(self, event: ft.ControlEvent) -> None:
        if self.storage is None:
            self.request(self.load_storage)
        else:
            self.request(lambda: self.load_folders(self.root, self.path))

    async def navigate(self, event: ft.ControlEvent) -> None:
        root, path = event.control.data
        self.request(lambda: self.load_folders(root, path))

    async def show_new_folder(self, event: ft.ControlEvent) -> None:
        if (
            self.closed
            or self.task is not None
            or self.picking
            or self.creating_folder
            or self.root is None
        ):
            return
        root, path = self.root, self.path
        self.creating_folder = True
        self.refresh()

        async def dismiss(event: ft.ControlEvent) -> None:
            self.creating_folder = False
            self.refresh()

        async def cancel(event: ft.ControlEvent) -> None:
            self.page.pop_dialog()

        async def create(event: ft.ControlEvent) -> None:
            name = (field.value or "").strip()
            if not valid_name(name):
                field.error_text = "Введите допустимое имя папки."
                field.update()
                return
            self.page.pop_dialog()
            self.request(lambda: self.create_folder(root, path, name))

        field = ft.TextField(label="Имя папки", autofocus=True, max_length=255, on_submit=create)
        self.page.show_dialog(
            ft.AlertDialog(
                modal=True,
                on_dismiss=dismiss,
                title=ft.Text("Новая папка"),
                content=field,
                actions=[
                    ft.TextButton("Отмена", on_click=cancel),
                    ft.FilledButton("Создать", on_click=create),
                ],
            )
        )

    async def create_folder(self, root: str, path: tuple[str, ...], name: str) -> None:
        await self.robot.create_folder(root, path, name)
        await self.load_folders(root, path)
        self.message(f"Создана папка «{name}».")

    async def receive_drop(self, event: DropEvent) -> None:
        await self.drop_paths(event.paths)

    async def drop_paths(self, paths: list[str]) -> None:
        if (
            self.closed
            or self.task is not None
            or self.picking
            or self.creating_folder
            or not paths
        ):
            return
        if self.root is None or self.storage is None:
            self.message("Выберите папку назначения перед отправкой.", warning=True)
            self.refresh()
            return
        root, path, max_bytes = self.root, self.path, self.storage.max_upload_bytes
        self.clear_file()
        self.sending, self.sent = True, 0
        self.request(lambda: self.upload_dropped(root, path, paths, max_bytes))

    async def upload_dropped(
        self, root: str, path: tuple[str, ...], paths: list[str], max_bytes: int
    ) -> None:
        copied = folders = 0
        failures = []
        try:
            for local in paths:
                source = Path(local)
                try:
                    if not source.is_absolute():
                        raise RobotError("Путь к файлу недоступен.", retryable=False)
                    entries = await asyncio.to_thread(lambda: list(drop_entries(source, max_bytes)))
                except OSError:
                    failures.append(f"«{source.name}» — не удалось прочитать.")
                    continue
                except RobotError as exc:
                    failures.append(f"«{source.name}» — {exc}")
                    continue

                blocked = set()
                for parts, info in entries:
                    name, relative_parent = parts[-1], parts[:-1]
                    directory = stat.S_ISDIR(info.st_mode)
                    if relative_parent in blocked:
                        if directory:
                            blocked.add(parts)
                        continue
                    parent = (*path, *relative_parent)
                    try:
                        if directory:
                            await self.robot.create_folder(root, parent, name, exist_ok=True)
                            if parent == path:
                                self.folders = sorted(
                                    {*self.folders, name},
                                    key=lambda value: (value.casefold(), value),
                                )
                            folders += 1
                        else:
                            file = ft.FilePickerFile(
                                id=0,
                                name=name,
                                size=info.st_size,
                                path=str(source.joinpath(*parts[1:])),
                            )
                            self.file, self.sent = file, 0
                            self.refresh()
                            await self.transfer(file, root, parent, (info.st_dev, info.st_ino))
                            self.clear_file()
                            copied += 1
                    except (OSError, RobotError) as exc:
                        description = (
                            str(exc) if isinstance(exc, RobotError) else "не удалось прочитать."
                        )
                        failures.append(f"«{name}» — {description}")
                        if directory:
                            blocked.add(parts)
        except asyncio.CancelledError:
            self.message("Отправка отменена. Проверьте содержимое папки на роботе.", warning=True)
            raise
        finally:
            self.clear_file()
        summary = f"Файлов отправлено: {copied}. Папок обработано: {folders}."
        if failures:
            summary += f" {failures[0]}"
            if len(failures) > 1:
                summary += f" Других ошибок: {len(failures) - 1}."
        self.message(summary, error=bool(failures))

    async def pick_file(self, event: ft.ControlEvent) -> None:
        if self.closed or self.picking or self.task is not None or self.storage is None:
            return
        self.picking = True
        self.message("")
        self.refresh()
        try:
            files = await self.picker.pick_files(allow_multiple=False, with_data=self.page.web)
            if not files:
                return
            chosen = files[0]
            if self.closed:
                self.release_cached_file(chosen)
                return
            self.clear_file()
            if chosen.size > self.storage.max_upload_bytes:
                self.release_cached_file(chosen)
                self.message(
                    f"Лимит робота — {file_size(self.storage.max_upload_bytes)}.", error=True
                )
            elif chosen.path is None and chosen.bytes is None:
                self.message(
                    "Системный выбор не дал доступ к файлу. Выберите другой файл.", error=True
                )
            else:
                self.file = chosen
        except (OSError, RuntimeError, TimeoutError, ValueError):
            self.message("Не удалось открыть системный выбор файла.", error=True)
        finally:
            self.picking = False
            self.refresh()

    async def start_upload(self, event: ft.ControlEvent) -> None:
        if (
            self.closed
            or self.task is not None
            or self.picking
            or self.file is None
            or self.root is None
        ):
            return
        file, root, path = self.file, self.root, self.path
        self.sending, self.sent = True, 0
        self.request(lambda: self.upload(file, root, path))

    async def cancel_upload(self, event: ft.ControlEvent) -> None:
        task = self.task
        if self.sending and task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            if self.task is task:
                self.task, self.sending = None, False
                self.message("Отправка отменена.", warning=True)
                self.refresh()

    async def transfer(
        self,
        file: ft.FilePickerFile,
        root: str,
        path: tuple[str, ...],
        identity: tuple[int, int] | None = None,
    ) -> FileReceipt:
        updated = 0.0

        async def content() -> AsyncIterator[bytes]:
            nonlocal updated
            async with aclosing(file_chunks(file, identity)) as source:
                async for chunk in source:
                    yield chunk
                    self.sent += len(chunk)
                    now = monotonic()
                    if now >= updated + 0.1 or self.sent == file.size:
                        updated = now
                        self.update_progress()

        async with aclosing(content()) as source:
            return await self.robot.upload_file(root, path, file.name, source, file.size)

    async def upload(self, file: ft.FilePickerFile, root: str, path: tuple[str, ...]) -> None:
        try:
            receipt = await self.transfer(file, root, path)
        except OSError:
            self.message("Не удалось прочитать файл. Выберите его заново.", error=True)
        except asyncio.CancelledError:
            self.message(
                "Отправка отменена. Проверьте папку на роботе."
                if self.sent >= file.size
                else "Отправка отменена.",
                warning=True,
            )
            raise
        else:
            self.clear_file()
            self.message(
                f"Сохранено на роботе: {' / '.join((receipt.root, *receipt.path, receipt.name))}"
            )

    def clear_file(self) -> None:
        if self.file is not None:
            self.release_cached_file(self.file)
            self.file = None

    def release_cached_file(self, file: ft.FilePickerFile) -> None:
        cache_dir = os.environ.get("FLET_APP_STORAGE_CACHE")
        if self.page.web or self.page.platform != ft.PagePlatform.ANDROID or not cache_dir:
            return
        if file.path is None:
            return
        with suppress(OSError, RuntimeError, ValueError):
            cache = Path(cache_dir).resolve() / "file_picker"
            path = Path(file.path).resolve(strict=True)
            parts = path.relative_to(cache).parts
            if len(parts) >= 2 and str(UUID(parts[0])) == parts[0]:
                path.unlink()
                path.parent.rmdir()

    def message(self, value: str, *, error: bool = False, warning: bool = False) -> None:
        self.notice.value = value
        color, tint = (
            (ERROR, ERROR_TINT)
            if error
            else (AMBER, AMBER_TINT)
            if warning
            else (
                SUCCESS,
                SUCCESS_TINT,
            )
        )
        self.notice.color = color
        self.feedback.bgcolor = tint
        self.notice.visible = self.feedback.visible = bool(value)

    def update_progress(self) -> None:
        if self.closed or self.file is None:
            return
        size = self.file.size
        ratio = min(1.0, self.sent / size) if size else 1.0
        self.progress.value = min(ratio, 0.99)
        self.progress_label.value = "Сохранение на роботе…" if self.sent >= size else f"{ratio:.0%}"
        self.progress_row.update()

    def refresh(self) -> None:
        if not self.closed:
            self.render()
            self.content.update()

    def render(self) -> None:
        busy = self.task is not None or self.picking
        self.set_compact(self.compact)
        self.reload.disabled = busy
        self.new_folder.disabled = busy or self.creating_folder or self.root is None
        self.drop_hint.visible = self.native_drop and self.root is not None and not busy
        if self.drop_zone is not None:
            self.drop_zone.accepting = self.drop_hint.visible and not self.creating_folder
        self.breadcrumb.controls = [
            ft.IconButton(
                ft.Icons.FOLDER_OUTLINED,
                icon_color=TEAL,
                tooltip="Рабочие папки",
                data=(None, ()),
                on_click=self.navigate,
                disabled=busy or self.root is None,
            ),
        ]
        if self.root is not None:
            for index, part in enumerate((self.root, *self.path)):
                crumb = (
                    ft.Text(part, color=TEAL, weight=ft.FontWeight.W_600)
                    if index == len(self.path)
                    else ft.TextButton(
                        part,
                        data=(self.root, self.path[:index]),
                        disabled=busy,
                        on_click=self.navigate,
                    )
                )
                self.breadcrumb.controls += [
                    ft.Icon(ft.Icons.CHEVRON_RIGHT, size=16, color=MUTED),
                    crumb,
                ]
        else:
            self.breadcrumb.controls.append(ft.Text("Рабочие папки", color=MUTED))
        entries = (
            [(name, (self.root, (*self.path, name))) for name in self.folders]
            if self.root is not None
            else [(root, (root, ())) for root in self.storage.roots]
            if self.storage
            else []
        )
        if entries:
            self.folders_view.controls = [
                ft.Container(
                    bgcolor=SURFACE,
                    border=ft.Border.only(bottom=ft.BorderSide(1, LINE)),
                    content=ft.ListTile(
                        leading=ft.Icon(ft.Icons.FOLDER_OUTLINED, color=TEAL, size=22),
                        title=ft.Text(
                            name,
                            color=TEXT,
                            max_lines=1,
                            overflow=ft.TextOverflow.ELLIPSIS,
                        ),
                        trailing=ft.Icon(ft.Icons.CHEVRON_RIGHT, color=MUTED, size=20),
                        content_padding=ft.Padding.symmetric(horizontal=10),
                        data=location,
                        disabled=busy,
                        on_click=self.navigate,
                    ),
                )
                for name, location in entries
            ]
        else:
            note = (
                "Загружаем папки…"
                if self.task is not None and not self.sending
                else "В этой папке нет вложенных папок."
                if self.root is not None
                else "Выберите рабочую папку."
                if self.storage is not None
                else "Папки робота пока недоступны."
            )
            self.folders_view.controls = [ft.Container(ft.Text(note, color=MUTED), padding=12)]
        self.pick.disabled = self.replace.disabled = busy or self.storage is None
        self.pick.visible = self.file is None and not self.sending
        self.replace.visible = self.file is not None and not self.sending
        self.selection.visible = self.file is not None
        self.filename.value = f"{self.file.name} · {file_size(self.file.size)}" if self.file else ""
        destination = (
            " / ".join((self.root, *self.path)) if self.root is not None else "Выберите папку"
        )
        self.destination.value = f"Куда: {destination}"
        self.send.visible = self.file is not None and not self.sending
        self.send.disabled = busy or self.root is None or self.file is None
        self.cancel.visible = self.sending
        self.actions.controls = [
            button for button in (self.pick, self.replace, self.send, self.cancel) if button.visible
        ]
        self.progress_row.visible = self.sending
        if self.sending and self.sent == 0:
            self.progress.value = 0
            self.progress_label.value = "Отправка…"
