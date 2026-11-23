import asyncio
import os
import stat
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import aclosing, suppress
from pathlib import Path
from time import monotonic
from uuid import UUID

import flet as ft

from client.connection import RobotConnection, RobotError
from common.protocol import FileStorage

SURFACE = "#222935"
BORDER = "#394352"
TEXT_MUTED = "#ADB8C8"
BLUE = "#0A9DE2"
ERROR = "#FFB4A9"
BLOCK = 128 * 1024


def file_size(size: int) -> str:
    if size < 1024:
        return f"{size} Б"
    if size < 1024**2:
        return f"{size / 1024:.0f} КБ"
    return f"{size / 1024**2:.1f} МБ"


def open_file(path: str, expected_size: int) -> int:
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
    fd = os.open(path, flags)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size != expected_size:
            raise OSError("The selected file changed.")
    except OSError:
        os.close(fd)
        raise
    return fd


async def file_chunks(file: ft.FilePickerFile) -> AsyncIterator[bytes]:
    if file.bytes is not None:
        if len(file.bytes) != file.size:
            raise OSError("The selected file changed.")
        for start in range(0, file.size, BLOCK):
            yield file.bytes[start : start + BLOCK]
        return
    if file.path is None:
        raise OSError("The selected file is unavailable.")
    fd = open_file(file.path, file.size)
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


class FilePanel:
    def __init__(
        self,
        page: ft.Page,
        robot: RobotConnection,
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
        self.sending = False
        self.sent = 0
        self.compact = compact

        self.back = ft.IconButton(ft.Icons.ARROW_BACK, tooltip="К запуску", on_click=back)
        self.reload = ft.IconButton(
            ft.Icons.REFRESH, tooltip="Обновить папки", on_click=self.reload_folders
        )
        self.breadcrumb = ft.Row(spacing=2, scroll=ft.ScrollMode.AUTO)
        self.folders_view = ft.ListView(expand=True, spacing=1)
        self.pick = ft.OutlinedButton(
            "Выбрать файл", icon=ft.Icons.UPLOAD_FILE_OUTLINED, on_click=self.pick_file
        )
        self.filename = ft.Text(expand=True, max_lines=1, overflow=ft.TextOverflow.ELLIPSIS)
        self.destination = ft.Text(color=TEXT_MUTED, max_lines=2, overflow=ft.TextOverflow.ELLIPSIS)
        self.send = ft.FilledButton(
            "Отправить",
            height=46,
            style=ft.ButtonStyle(
                bgcolor={ft.ControlState.DEFAULT: BLUE, ft.ControlState.DISABLED: "#343C4A"},
                color={ft.ControlState.DEFAULT: "#101B27", ft.ControlState.DISABLED: TEXT_MUTED},
                shape=ft.RoundedRectangleBorder(radius=8),
            ),
            on_click=self.start_upload,
        )
        self.cancel = ft.OutlinedButton("Отмена", height=44, on_click=self.cancel_upload)
        self.progress = ft.ProgressBar(expand=True, value=0, color=BLUE, bar_height=6)
        self.progress_label = ft.Text(size=13)
        self.notice = ft.Text(size=13, selectable=True, visible=False)
        self.summary = ft.ResponsiveRow(
            [
                ft.Row(
                    [ft.Icon(ft.Icons.DESCRIPTION_OUTLINED, size=21), self.filename],
                    col={"xs": 12, "md": 4},
                ),
                ft.Container(self.destination, col={"xs": 12, "md": 5}),
                ft.Row(
                    [self.send, self.cancel],
                    col={"xs": 12, "md": 3},
                    spacing=8,
                    alignment=ft.MainAxisAlignment.END,
                ),
            ],
            spacing=10,
            run_spacing=8,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )
        self.progress_row = ft.Row([self.progress, self.progress_label], spacing=10)
        self.tray = ft.Container(
            bgcolor=SURFACE,
            border=ft.Border.all(1, BORDER),
            border_radius=8,
            padding=12,
            content=ft.Column(
                [self.pick, self.summary, self.progress_row, self.notice],
                spacing=9,
                horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
            ),
        )
        self.content = ft.Container(
            expand=True,
            padding=ft.Padding.all(16),
            content=ft.Column(
                [
                    ft.Row(
                        [
                            self.back,
                            ft.Text(
                                "Папки робота", size=25, weight=ft.FontWeight.W_600, expand=True
                            ),
                            self.reload,
                        ],
                        spacing=3,
                    ),
                    ft.Container(
                        bgcolor=SURFACE,
                        border=ft.Border.all(1, BORDER),
                        border_radius=8,
                        padding=ft.Padding.symmetric(horizontal=8, vertical=2),
                        content=self.breadcrumb,
                    ),
                    self.folders_view,
                    self.tray,
                ],
                spacing=12,
                horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
            ),
        )
        self.render()

    def set_compact(self, compact: bool) -> None:
        self.compact = compact
        self.back.visible = compact
        self.content.padding = ft.Padding.all(10 if compact else 18)

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
                self.message("Отправка отменена.")
                self.refresh()

    async def upload(self, file: ft.FilePickerFile, root: str, path: tuple[str, ...]) -> None:
        updated = 0.0

        async def content() -> AsyncIterator[bytes]:
            nonlocal updated
            async with aclosing(file_chunks(file)) as source:
                async for chunk in source:
                    yield chunk
                    self.sent += len(chunk)
                    now = monotonic()
                    if now >= updated + 0.1 or self.sent == file.size:
                        updated = now
                        self.update_progress()

        try:
            async with aclosing(content()) as source:
                receipt = await self.robot.upload_file(root, path, file.name, source, file.size)
        except OSError:
            self.message("Не удалось прочитать файл. Выберите его заново.", error=True)
        except asyncio.CancelledError:
            self.message(
                "Отправка отменена. Проверьте папку на роботе."
                if self.sent >= file.size
                else "Отправка отменена."
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

    def message(self, value: str, *, error: bool = False) -> None:
        self.notice.value = value
        self.notice.color = ERROR if error else "#9CE4D0"
        self.notice.visible = bool(value)

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
        self.breadcrumb.controls = [
            ft.IconButton(
                ft.Icons.FOLDER_OUTLINED,
                tooltip="Рабочие папки",
                data=(None, ()),
                on_click=self.navigate,
                disabled=busy or self.root is None,
            ),
        ]
        if self.root is not None:
            for index, part in enumerate((self.root, *self.path)):
                self.breadcrumb.controls += [
                    ft.Icon(ft.Icons.CHEVRON_RIGHT, size=16, color=TEXT_MUTED),
                    ft.TextButton(
                        part,
                        data=(self.root, self.path[:index]),
                        disabled=busy or index == len(self.path),
                        on_click=self.navigate,
                    ),
                ]
        else:
            self.breadcrumb.controls.append(ft.Text("Рабочие папки", color=TEXT_MUTED))
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
                    border_radius=6,
                    content=ft.ListTile(
                        leading=ft.Icon(ft.Icons.FOLDER_OUTLINED, size=24),
                        title=ft.Text(name, max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
                        trailing=ft.Icon(ft.Icons.CHEVRON_RIGHT, color=TEXT_MUTED, size=22),
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
                if busy
                else "В этой папке нет вложенных папок."
                if self.root is not None
                else "Выберите рабочую папку."
                if self.storage is not None
                else "Папки робота пока недоступны."
            )
            self.folders_view.controls = [ft.Container(ft.Text(note, color=TEXT_MUTED), padding=12)]
        self.pick.disabled = busy or self.storage is None
        self.pick.visible = not self.sending
        self.pick.content = "Выбрать другой файл" if self.file is not None else "Выбрать файл"
        self.filename.value = (
            f"{self.file.name} · {file_size(self.file.size)}" if self.file else "Файл не выбран"
        )
        destination = (
            " / ".join((self.root, *self.path)) if self.root is not None else "Выберите папку"
        )
        self.destination.value = f"Куда: {destination}"
        self.send.visible = not self.sending
        self.send.disabled = busy or self.root is None or self.file is None
        self.cancel.visible = self.sending
        self.progress_row.visible = self.sending
        if self.sending and self.sent == 0:
            self.progress.value = 0
            self.progress_label.value = "Отправка…"
