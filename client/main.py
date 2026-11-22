import argparse
import asyncio
from collections.abc import Awaitable, Callable
from contextlib import aclosing, suppress
from typing import TypeVar
from uuid import UUID

import flet as ft

from client.connection import RobotConnection, RobotError
from common.protocol import LOG_LIMIT, EventFrame, RunState, Script

T = TypeVar("T")

STATUS = {
    "running": "Выполняется",
    "stopping": "Останавливается…",
    "succeeded": "Завершён",
    "stopped": "Остановлен",
    "failed": "Завершён с ошибкой",
}
PANEL = "#151F2D"
MUTED = "#A7B7CB"
ACCENT = "#70E0CD"


class Dashboard:
    def __init__(self, page: ft.Page) -> None:
        self.page = page
        self.task: asyncio.Task[None] | None = None
        self.robot: RobotConnection | None = None
        self.current: RunState | None = None
        self.cursor = 0
        self.run_id: UUID | None = None
        self.connected = False
        self.pending = False
        self.closed = False
        self.scripts: dict[str, Script] = {}

        self.address = ft.TextField(label="Адрес агента", value="http://127.0.0.1:8765")
        self.token = ft.TextField(label="Токен агента", password=True, can_reveal_password=True)
        self.connection_button = ft.OutlinedButton("Подключиться", on_click=self.toggle_connection)
        self.connection_status = ft.Text("Нет подключения", color=MUTED)
        self.selector = ft.Dropdown(label="Скрипт", disabled=True, on_select=self.select_script)
        self.description = ft.Text(color=MUTED)
        self.start_button = ft.FilledButton("Запустить", disabled=True, on_click=self.start)
        self.stop_button = ft.OutlinedButton("Остановить", disabled=True, on_click=self.stop)
        self.run_status = ft.Text("Запусков пока нет.", color=MUTED)
        self.error = ft.Text(color="#FFB4A9", visible=False)
        self.empty_log = ft.Text("Сообщения появятся после запуска.", color=MUTED)
        self.log_gap = ft.Text("Часть предыдущих сообщений пропущена.", color=MUTED, visible=False)
        self.log = ft.ListView(expand=True, auto_scroll=True, spacing=4)
        self.log_card = self.card(
            ft.Text("Журнал", size=17, weight=ft.FontWeight.W_600),
            self.empty_log,
            self.log_gap,
            ft.SelectionArea(content=self.log, expand=True),
        )

    def mount(self) -> None:
        self.page.title = "RoboLab"
        self.page.theme_mode = ft.ThemeMode.DARK
        self.page.theme = ft.Theme(color_scheme_seed=ACCENT)
        self.page.bgcolor = "#0D1623"
        self.page.padding = 18
        self.page.on_close = self.close
        self.page.on_resize = self.resize
        self.size_log()

        heading = ft.Row(
            [
                ft.Text("RoboLab", size=26, weight=ft.FontWeight.BOLD),
                self.connection_status,
            ],
            alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
            wrap=True,
        )
        connection_panel = self.card(
            ft.Text("Подключение", size=17, weight=ft.FontWeight.W_600),
            self.address,
            self.token,
            self.connection_button,
        )
        run_panel = self.card(
            ft.Text("Запуск", size=17, weight=ft.FontWeight.W_600),
            self.selector,
            self.description,
            ft.Row([self.start_button, self.stop_button], wrap=True),
            self.run_status,
            self.error,
        )
        self.page.add(
            ft.Column(
                [
                    heading,
                    ft.ResponsiveRow(
                        [
                            ft.Container(connection_panel, col={"xs": 12, "md": 6}),
                            ft.Container(run_panel, col={"xs": 12, "md": 6}),
                        ],
                        spacing=12,
                        run_spacing=12,
                    ),
                    self.log_card,
                ],
                expand=True,
                spacing=12,
                scroll=ft.ScrollMode.AUTO,
            )
        )

    @staticmethod
    def card(*controls: ft.Control) -> ft.Container:
        return ft.Container(
            content=ft.Column(list(controls), spacing=10),
            padding=16,
            border_radius=12,
            bgcolor=PANEL,
        )

    def size_log(self) -> None:
        stacked = (self.page.width or 0) < 804
        self.log_card.height = max(230, (self.page.height or 720) - (535 if stacked else 330))

    async def resize(self, event: ft.ControlEvent) -> None:
        self.size_log()
        self.page.update()

    async def toggle_connection(self, event: ft.ControlEvent) -> None:
        if self.task is not None:
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task
            self.task = None
            self.connection_status.value = "Отключено"
            self.refresh()
            return
        self.set_error("")
        try:
            self.robot = RobotConnection(self.address.value or "", (self.token.value or "").strip())
        except ValueError as exc:
            self.set_error(str(exc))
            self.refresh()
            return
        self.scripts = {}
        self.selector.options = []
        self.selector.value = None
        self.current, self.run_id, self.cursor = None, None, 0
        self.log.controls.clear()
        self.log_gap.visible = False
        self.connection_status.value = "Подключение…"
        self.task = asyncio.create_task(self.watch(self.robot))
        self.refresh()

    async def watch(self, robot: RobotConnection) -> None:
        delay = 1
        try:
            while True:
                try:
                    scripts = await robot.catalog()
                    self.scripts = {script.id: script for script in scripts}
                    self.selector.options = [
                        ft.DropdownOption(key=s.id, text=s.name) for s in scripts
                    ]
                    if self.selector.value not in self.scripts:
                        self.selector.value = scripts[0].id if scripts else None
                    async with aclosing(robot.events(self.run_id, self.cursor)) as events:
                        async for packet in events:
                            self.connected = True
                            delay = 1
                            self.connection_status.value = "Mock Robot · онлайн"
                            self.show_frame(packet)
                            self.refresh()
                except RobotError as exc:
                    self.connected = False
                    if not exc.retryable:
                        self.connection_status.value = "Нет подключения"
                        self.set_error(str(exc))
                        return
                    self.connection_status.value = f"{exc} Повтор через {delay} с"
                    self.refresh()
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 8)
        finally:
            self.robot = None
            self.task = None
            self.connected = False
            self.refresh()

    def show_frame(self, packet: EventFrame) -> None:
        self.current = packet.run
        run_id = packet.run.id if packet.run else None
        if run_id != self.run_id:
            self.run_id, self.cursor = run_id, 0
            self.log.controls.clear()
            self.log_gap.visible = False
        if packet.gap:
            self.log_gap.visible = True
        for line in packet.logs:
            stamp = line.timestamp.astimezone().strftime("%H:%M:%S")
            self.log.controls.append(
                ft.Text(
                    f"{stamp}  {line.level}  {line.source}  {line.message}",
                    size=13,
                )
            )
            self.cursor = line.sequence
        if len(self.log.controls) > LOG_LIMIT:
            del self.log.controls[:-LOG_LIMIT]

    def refresh(self) -> None:
        if self.closed:
            return
        watching = self.task is not None
        current = self.current
        busy = current is not None and current.status in ("running", "stopping")
        self.address.disabled = watching
        self.token.disabled = watching
        self.connection_button.content = "Отключиться" if watching else "Подключиться"
        self.connection_button.disabled = self.pending
        self.connection_status.color = ACCENT if self.connected else MUTED
        self.selector.disabled = not self.connected or busy or self.pending
        selected = self.scripts.get(self.selector.value or "")
        self.description.value = selected.description if selected else ""
        self.start_button.disabled = not self.connected or busy or self.pending or selected is None
        self.stop_button.disabled = (
            not self.connected or self.pending or current is None or current.status != "running"
        )
        self.empty_log.visible = not self.log.controls
        self.run_status.value = self.format_run(current)
        self.page.update()

    def format_run(self, current: RunState | None) -> str:
        if current is None:
            return "Запусков пока нет."
        fields = [STATUS[current.status], f"{current.elapsed_seconds:.1f} с"]
        if current.pid is not None:
            fields.append(f"PID {current.pid}")
        if current.exit_code is not None:
            fields.append(f"Код {current.exit_code}")
        return " · ".join(fields)

    def set_error(self, message: str) -> None:
        self.error.value = message
        self.error.visible = bool(message)

    async def select_script(self, event: ft.ControlEvent) -> None:
        self.refresh()

    async def start(self, event: ft.ControlEvent) -> None:
        if self.robot and self.selector.value and not self.start_button.disabled:
            await self.command(self.robot.start, self.selector.value)

    async def stop(self, event: ft.ControlEvent) -> None:
        if self.robot and self.current and not self.stop_button.disabled:
            await self.command(self.robot.stop, self.current.id)

    async def command(self, action: Callable[[T], Awaitable[None]], value: T) -> None:
        self.pending = True
        self.set_error("")
        self.refresh()
        try:
            await action(value)
        except RobotError as exc:
            self.set_error(str(exc))
        finally:
            self.pending = False
            self.refresh()

    async def close(self, event: ft.ControlEvent) -> None:
        self.closed = True
        if self.task is not None:
            self.task.cancel()


async def main(page: ft.Page) -> None:
    Dashboard(page).mount()


def run() -> None:
    parser = argparse.ArgumentParser(prog="robolab-client")
    parser.add_argument("--web", action="store_true")
    parser.add_argument("--port", type=int, default=8550)
    args = parser.parse_args()
    if args.web:
        ft.run(main, view=ft.AppView.WEB_BROWSER, host="127.0.0.1", port=args.port)
    else:
        ft.run(main)


if __name__ == "__main__":
    run()
