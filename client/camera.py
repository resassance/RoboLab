import asyncio
from collections.abc import Callable
from uuid import UUID

import flet as ft

from client.appearance import (
    AMBER,
    AMBER_TINT,
    CONTROL_RADIUS,
    ERROR,
    LINE,
    MUTED,
    SUCCESS,
    SURFACE,
    TEAL,
    TEAL_TINT,
    TEXT,
    button_row,
)
from client.connection import CameraFrame, RobotConnection, RobotError
from common.protocol import CameraOptions

VIEWS = {
    "original": "Исходное изображение",
    "gray": "Оттенки серого",
    "mask": "Маска",
    "overlay": "Разметка",
}


class CameraPanel:
    def __init__(
        self,
        page: ft.Page,
        robot: RobotConnection,
        picker: ft.FilePicker,
        on_closed: Callable[["CameraPanel"], None],
    ) -> None:
        self.page = page
        self.robot = robot
        self.picker = picker
        self.on_closed = on_closed
        self.task: asyncio.Task[None] | None = None
        self.frame: CameraFrame | None = None
        self.closed = False
        self.pulling = False
        self.stopping = False
        self.saving = False
        self.run_active = False
        self.compact = False

        defaults = CameraOptions()
        self.view = ft.Dropdown(
            label="Вид изображения",
            value=defaults.view,
            options=[ft.DropdownOption(key=key, text=label) for key, label in VIEWS.items()],
        )
        self.fps = ft.Slider(
            min=1,
            max=8,
            divisions=7,
            value=defaults.fps,
            label="{value}",
            on_change=self.update_settings,
        )
        self.quality = ft.Slider(
            min=35,
            max=90,
            divisions=55,
            value=defaults.quality,
            label="{value}",
            on_change=self.update_settings,
        )
        self.fps_label = ft.Text(f"Частота · {defaults.fps} FPS")
        self.quality_label = ft.Text(f"Качество JPEG · {defaults.quality}")
        self.image = ft.Image(
            src=b"",
            fit=ft.BoxFit.CONTAIN,
            gapless_playback=True,
            visible=False,
            semantics_label="Кадр с камеры робота",
        )
        self.empty = ft.Container(
            content=ft.Column(
                [
                    ft.Icon(ft.Icons.VIDEOCAM_OUTLINED, color=SURFACE, size=30),
                    ft.Text("Кадра пока нет", color=SURFACE, size=14),
                ],
                tight=True,
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                spacing=8,
            ),
            alignment=ft.Alignment.CENTER,
        )
        self.status = ft.Text("Просмотр выключен.", color=MUTED)
        self.compact_status = ft.Text(
            self.status.value,
            color=MUTED,
            size=11,
            max_lines=1,
            overflow=ft.TextOverflow.ELLIPSIS,
            text_align=ft.TextAlign.RIGHT,
            expand=True,
            visible=False,
        )
        self.status_strip = ft.Container(
            self.status,
            bgcolor=TEAL_TINT,
            border_radius=CONTROL_RADIUS,
            padding=ft.Padding.symmetric(horizontal=8, vertical=6),
        )
        self.info = ft.Text(color=MUTED, size=13, visible=False)
        self.error = ft.Text(color=ERROR, visible=False)
        self.file_notice = ft.Text(size=14, visible=False)
        self.run_notice = ft.Container(
            ft.Text(size=13, color=AMBER),
            bgcolor=AMBER_TINT,
            border_radius=CONTROL_RADIUS,
            padding=ft.Padding.symmetric(horizontal=8, vertical=6),
            visible=False,
        )
        action_style = ft.ButtonStyle(
            padding=ft.Padding.symmetric(horizontal=4),
            shape=ft.RoundedRectangleBorder(radius=CONTROL_RADIUS),
        )
        self.start_button = ft.FilledButton(
            ft.Text("Запустить", size=12, text_align=ft.TextAlign.CENTER),
            tooltip="Начать просмотр камеры",
            height=44,
            on_click=self.start,
            style=action_style,
        )
        self.stop_button = ft.OutlinedButton(
            ft.Text("Стоп", size=12, text_align=ft.TextAlign.CENTER),
            tooltip="Остановить просмотр камеры",
            height=44,
            disabled=True,
            on_click=self.stop,
            style=action_style,
        )
        self.save_button = ft.OutlinedButton(
            ft.Text("Сохранить", size=12, text_align=ft.TextAlign.CENTER),
            tooltip="Сохранить кадр JPEG",
            height=44,
            disabled=True,
            on_click=self.save,
            style=action_style,
        )
        self.preview = ft.Container(
            height=245,
            bgcolor=TEXT,
            border_radius=CONTROL_RADIUS,
            clip_behavior=ft.ClipBehavior.HARD_EDGE,
            content=ft.Stack([self.empty, self.image], fit=ft.StackFit.EXPAND),
        )
        settings = ft.ExpansionTile(
            title=ft.Text("Параметры камеры", size=13, weight=ft.FontWeight.W_600),
            controls=[
                self.info,
                self.view,
                ft.ResponsiveRow(
                    [
                        ft.Column([self.fps_label, self.fps], spacing=2, col={"xs": 12, "sm": 6}),
                        ft.Column(
                            [self.quality_label, self.quality], spacing=2, col={"xs": 12, "sm": 6}
                        ),
                    ],
                    spacing=10,
                    run_spacing=6,
                ),
            ],
            expanded_cross_axis_alignment=ft.CrossAxisAlignment.STRETCH,
            tile_padding=ft.Padding.symmetric(horizontal=8),
            min_tile_height=32,
            maintain_state=True,
        )
        self.content = ft.Container(
            content=ft.Column(
                [
                    ft.Row(
                        [
                            ft.Icon(ft.Icons.VIDEOCAM_OUTLINED, size=21, color=TEAL),
                            ft.Text("Камера", size=21, weight=ft.FontWeight.W_700, color=TEXT),
                            self.compact_status,
                        ],
                        spacing=8,
                    ),
                    self.preview,
                    button_row(self.start_button, self.stop_button, self.save_button, spacing=6),
                    self.status_strip,
                    self.run_notice,
                    self.error,
                    self.file_notice,
                    settings,
                ],
                horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
                spacing=8,
            ),
            bgcolor=SURFACE,
            border=ft.Border.all(1, LINE),
            border_radius=CONTROL_RADIUS,
            padding=12,
        )

    def set_compact(self, compact: bool) -> None:
        self.compact = compact
        self.preview.height = 132 if compact else 245
        self.update_controls()

    def set_run_active(self, active: bool) -> None:
        self.run_active = active
        self.update_controls()

    async def pause_for_run(self) -> None:
        task = self.task
        if task is not None:
            self.status.value = "Останавливаем просмотр…"
            self.request_stop()
            self.refresh()
            await asyncio.shield(task)

    def refresh(self) -> None:
        if self.closed:
            return
        self.update_controls()
        if self.content.parent is not None:
            self.content.update()

    def update_controls(self) -> None:
        running = self.task is not None
        self.view.disabled = self.fps.disabled = self.quality.disabled = running
        self.start_button.disabled = running or self.run_active
        self.stop_button.disabled = not running or self.stopping
        self.save_button.disabled = self.frame is None or self.saving
        self.status.color = TEAL if running and not self.stopping else MUTED
        show_status = running or not self.run_active
        self.status_strip.visible = show_status and not self.compact
        self.compact_status.visible = show_status and self.compact
        self.compact_status.value = self.status.value
        self.compact_status.color = self.status.color
        self.run_notice.visible = self.run_active
        self.run_notice.content.value = (
            "Во время аппаратного запуска показан последний кадр."
            if self.frame is not None
            else "Просмотр камеры доступен после завершения аппаратного запуска."
        )

    async def update_settings(self, event: ft.ControlEvent) -> None:
        self.fps_label.value = f"Частота · {self.fps.value:.0f} FPS"
        self.quality_label.value = f"Качество JPEG · {self.quality.value:.0f}"
        self.refresh()

    def show_frame(self, frame: CameraFrame | None) -> None:
        self.frame = frame
        self.empty.visible = frame is None
        self.image.visible = frame is not None
        self.image.src = frame.jpeg if frame else b""
        self.info.visible = frame is not None
        self.info.value = (
            f"Кадр №{frame.sequence} · Захват {frame.fps:.1f} FPS\n"
            f"Возраст при выдаче {frame.age_ms:.0f} мс · Запрос HTTP {frame.request_ms:.0f} мс"
            if frame
            else ""
        )

    async def start(self, event: ft.ControlEvent) -> None:
        if self.closed or self.task is not None or self.run_active:
            return
        self.show_frame(None)
        self.error.visible = self.file_notice.visible = False
        try:
            options = CameraOptions.model_validate(
                {
                    "view": self.view.value,
                    "fps": round(self.fps.value),
                    "quality": round(self.quality.value),
                }
            )
        except (TypeError, ValueError):
            self.set_error("Проверьте параметры просмотра.")
        else:
            self.status.value = "Включаем просмотр…"
            self.task = asyncio.create_task(self.stream(options))
        self.refresh()

    async def stream(self, options: CameraOptions) -> None:
        session_id = None
        try:
            session_id = await self.robot.start_camera(options)
            if not self.stopping:
                self.pulling = True
                await self.pull(session_id, options.fps)
        except asyncio.CancelledError:
            if not self.stopping:
                raise
        except RobotError as exc:
            if not self.stopping:
                self.show_frame(None)
                self.status.value = "Просмотр остановлен."
                self.set_error(str(exc))
                self.refresh()
        finally:
            self.pulling = False
            try:
                if session_id is not None:
                    await self.robot.stop_camera(session_id)
            except RobotError as exc:
                if self.stopping and not self.closed:
                    self.set_error(str(exc))
            finally:
                self.task = None
                if self.closed:
                    self.on_closed(self)
                else:
                    if self.stopping:
                        self.status.value = (
                            "Просмотр остановлен. Показан последний кадр."
                            if self.frame
                            else "Просмотр остановлен."
                        )
                    self.stopping = False
                    self.refresh()

    async def pull(self, session_id: UUID, fps: int) -> None:
        after = 0
        loop = asyncio.get_running_loop()
        self.status.value = "Ждём первый кадр…"
        self.refresh()
        async with self.robot.http_client() as http:
            while True:
                sent_at = loop.time()
                frame = await self.robot.camera_frame(http, session_id, after)
                if frame is not None:
                    after = frame.sequence
                    self.show_frame(frame)
                    self.status.value = "Идёт просмотр."
                    self.refresh()
                await asyncio.sleep(max(0, sent_at + 1 / fps - loop.time()))

    def set_error(self, message: str) -> None:
        self.error.value = message
        self.error.visible = True

    def request_stop(self) -> None:
        if self.task is not None and not self.stopping:
            self.stopping = True
            if self.pulling:
                self.task.cancel()

    async def stop(self, event: ft.ControlEvent) -> None:
        if self.closed or self.task is None or self.stopping:
            return
        self.status.value = "Останавливаем просмотр…"
        self.request_stop()
        self.refresh()

    def close(self, *, update: bool) -> None:
        if self.closed:
            return
        self.closed = True
        self.request_stop()
        self.show_frame(None)
        self.content.visible = False
        if update and self.content.parent is not None:
            self.content.update()
        if self.task is None:
            self.on_closed(self)

    async def save(self, event: ft.ControlEvent) -> None:
        frame = self.frame
        if self.closed or frame is None or self.saving:
            return
        self.saving = True
        self.file_notice.visible = False
        self.refresh()
        try:
            path = await self.picker.save_file(
                dialog_title="Сохранить кадр",
                file_name=f"robolab-camera-{frame.sequence}.jpg",
                src_bytes=frame.jpeg,
                file_type=ft.FilePickerFileType.CUSTOM,
                allowed_extensions=["jpg", "jpeg"],
            )
        except (OSError, RuntimeError, TimeoutError, ValueError):
            self.file_notice.value = "Не удалось сохранить JPEG на этом устройстве."
            self.file_notice.color = ERROR
            self.file_notice.visible = True
        else:
            if path is not None or self.page.web:
                self.file_notice.value = (
                    "Загрузка передана браузеру." if self.page.web else "Кадр сохранён."
                )
                self.file_notice.color = SUCCESS
                self.file_notice.visible = True
        finally:
            self.saving = False
            self.refresh()
