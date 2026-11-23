import asyncio
from collections.abc import Callable
from uuid import UUID

import flet as ft

from client.connection import CameraFrame, RobotConnection, RobotError
from common.protocol import CameraOptions

VIEWS = {
    "original": "Исходное изображение",
    "gray": "Оттенки серого",
    "mask": "Маска",
    "overlay": "Разметка",
}


class CameraDialog:
    def __init__(
        self,
        page: ft.Page,
        robot: RobotConnection,
        picker: ft.FilePicker,
        on_closed: Callable[["CameraDialog"], None],
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

        defaults = CameraOptions()
        self.view = ft.RadioGroup(
            value=defaults.view,
            content=ft.Column(
                [
                    ft.Text("Вид"),
                    ft.Row(
                        [ft.Radio(value=key, label=label) for key, label in VIEWS.items()],
                        wrap=True,
                    ),
                ],
                spacing=2,
            ),
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
        self.empty = ft.Container(content=ft.Text("Кадра пока нет"), alignment=ft.Alignment.CENTER)
        self.status = ft.Text("Просмотр выключен.", color="#A7B7CB")
        self.info = ft.Text(color="#A7B7CB", size=12, visible=False)
        self.error = ft.Text(color="#FFB4A9", visible=False)
        self.file_notice = ft.Text(visible=False)
        self.start_button = ft.FilledButton("Начать просмотр", on_click=self.start)
        self.stop_button = ft.OutlinedButton("Остановить", disabled=True, on_click=self.stop)
        self.save_button = ft.OutlinedButton("Сохранить JPEG", disabled=True, on_click=self.save)
        self.dialog = ft.AlertDialog(
            modal=True,
            title=ft.Text("Камера"),
            content=ft.Container(
                width=560,
                content=ft.Column(
                    [
                        ft.Container(
                            height=min(300, max(140, (page.height or 720) * 0.38)),
                            bgcolor="#0D1623",
                            border_radius=8,
                            clip_behavior=ft.ClipBehavior.HARD_EDGE,
                            content=ft.Stack([self.empty, self.image], fit=ft.StackFit.EXPAND),
                        ),
                        self.info,
                        self.status,
                        self.error,
                        self.view,
                        ft.Row(
                            [
                                ft.Column([self.fps_label, self.fps], spacing=2, expand=True),
                                ft.Column(
                                    [self.quality_label, self.quality], spacing=2, expand=True
                                ),
                            ],
                            vertical_alignment=ft.CrossAxisAlignment.START,
                        ),
                        ft.Row([self.start_button, self.stop_button, self.save_button], wrap=True),
                        self.file_notice,
                    ],
                    horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
                    spacing=10,
                ),
            ),
            scrollable=True,
            actions=[ft.TextButton("Закрыть", on_click=self.dismiss)],
            on_dismiss=self.dismissed,
        )

    def show(self) -> None:
        self.page.show_dialog(self.dialog)

    def refresh(self) -> None:
        if self.closed:
            return
        running = self.task is not None
        self.view.disabled = self.fps.disabled = self.quality.disabled = running
        self.start_button.disabled = running
        self.stop_button.disabled = not running or self.stopping
        self.save_button.disabled = self.frame is None or self.saving
        self.dialog.update()

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
        if self.closed or self.task is not None:
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
        was_open, self.dialog.open = self.dialog.open, False
        if update and was_open:
            self.dialog.update()
        if self.task is None:
            self.on_closed(self)

    async def dismiss(self, event: ft.ControlEvent) -> None:
        self.close(update=True)

    async def dismissed(self, event: ft.ControlEvent) -> None:
        self.close(update=False)

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
            self.file_notice.color = "#FFB4A9"
            self.file_notice.visible = True
        else:
            if path is not None or self.page.web:
                self.file_notice.value = (
                    "Загрузка передана браузеру." if self.page.web else "Кадр сохранён."
                )
                self.file_notice.color = "#70E0CD"
                self.file_notice.visible = True
        finally:
            self.saving = False
            self.refresh()
