import argparse
import asyncio
from collections.abc import Awaitable, Callable
from contextlib import aclosing, suppress
from uuid import UUID

import flet as ft

from client.camera import CameraDialog
from client.configuration import ConfigurationStore, ParameterDialog
from client.connection import RobotConnection, RobotError
from common.protocol import LOG_LIMIT, EventFrame, RunState, SafetyState, Script

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
ERROR = "#FFB4A9"
HEARTBEAT_PERIOD = 0.7
LEASE_TIMEOUT = 4


class Dashboard:
    def __init__(self, page: ft.Page) -> None:
        self.page = page
        self.task: asyncio.Task[None] | None = None
        self.heartbeat_task: asyncio.Task[None] | None = None
        self.robot: RobotConnection | None = None
        self.current: RunState | None = None
        self.safety: SafetyState | None = None
        self.safety_pending: RobotConnection | None = None
        self.event_generation = 0
        self.lease_generation = 0
        self.leased_run: UUID | None = None
        self.cursor = 0
        self.run_id: UUID | None = None
        self.connected = False
        self.pending = False
        self.closed = False
        self.page_available = True
        self.scripts: dict[str, Script] = {}
        self.configurations = ConfigurationStore()
        self.camera: CameraDialog | None = None
        self.camera_picker = ft.FilePicker()

        self.address = ft.TextField(label="Адрес агента", value="http://127.0.0.1:8765")
        self.token = ft.TextField(label="Токен агента", password=True, can_reveal_password=True)
        self.connection_button = ft.OutlinedButton("Подключиться", on_click=self.toggle_connection)
        self.connection_status = ft.Text("Нет подключения", color=MUTED)
        self.camera_button = ft.OutlinedButton("Камера", visible=False, on_click=self.open_camera)
        self.selector = ft.Dropdown(label="Скрипт", disabled=True, on_select=self.select_script)
        self.description = ft.Text(color=MUTED)
        self.parameters_button = ft.OutlinedButton(
            "Параметры", disabled=True, on_click=self.configure
        )
        self.start_button = ft.FilledButton("Запустить", disabled=True, on_click=self.start)
        self.stop_button = ft.OutlinedButton("Остановить", disabled=True, on_click=self.stop)
        self.run_status = ft.Text("Запусков пока нет.", color=MUTED)
        self.emergency_button = ft.FilledButton(
            "Аварийная остановка",
            style=ft.ButtonStyle(bgcolor="#B3261E", color=ft.Colors.WHITE),
            disabled=True,
            on_click=self.emergency_stop,
        )
        self.reset_button = ft.OutlinedButton(
            "Снять блокировку", disabled=True, on_click=self.reset_safety
        )
        self.safety_status = ft.Text(color=MUTED)
        self.safety_controls = ft.Column(
            [
                ft.Row([self.emergency_button, self.reset_button], wrap=True),
                self.safety_status,
            ],
            visible=False,
            spacing=6,
        )
        self.error = ft.Text(color=ERROR, visible=False)
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
        self.page.on_connect = self.reconnect_page
        self.page.on_disconnect = self.disconnect_page
        self.page.on_close = self.close
        self.page.on_resize = self.resize
        self.page.services.append(self.camera_picker)
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
            ft.Row([self.connection_button, self.camera_button], wrap=True),
        )
        run_panel = self.card(
            ft.Text("Запуск", size=17, weight=ft.FontWeight.W_600),
            self.selector,
            self.description,
            ft.Row([self.parameters_button, self.start_button, self.stop_button], wrap=True),
            self.run_status,
            self.safety_controls,
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
        if self.closed or not self.page_available:
            return
        if self.task is not None:
            await self.disconnect()
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

    async def disconnect(self) -> None:
        task, self.task = self.task, None
        self.drop_lease()
        self.robot = None
        self.connected = False
        self.safety = None
        self.connection_status.value = "Отключено"
        self.close_camera()
        if task is not None:
            task.cancel()
        self.refresh()
        if task is not None:
            with suppress(asyncio.CancelledError):
                await task

    async def watch(self, robot: RobotConnection) -> None:
        delay = 1
        try:
            while True:
                try:
                    scripts = await robot.catalog()
                    if not robot.camera_available:
                        self.close_camera()
                    self.scripts = {script.id: script for script in scripts}
                    self.selector.options = [
                        ft.DropdownOption(key=s.id, text=s.name) for s in scripts
                    ]
                    if self.selector.value not in self.scripts:
                        self.selector.value = scripts[0].id if scripts else None
                    generation = self.event_generation
                    async with aclosing(robot.events(self.run_id, self.cursor)) as events:
                        async for packet in events:
                            if generation != self.event_generation:
                                break
                            self.connected = True
                            delay = 1
                            name = "Yahboom" if robot.kind == "yahboom" else "Mock Robot"
                            self.connection_status.value = f"{name} · онлайн"
                            self.show_frame(packet)
                            self.refresh()
                except RobotError as exc:
                    self.connected = False
                    self.close_camera()
                    if robot.kind == "yahboom":
                        self.drop_lease()
                        self.safety = None
                    if not exc.retryable:
                        self.connection_status.value = "Нет подключения"
                        self.set_error(str(exc))
                        return
                    self.connection_status.value = f"{exc} Повтор через {delay} с"
                    self.refresh()
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 8)
        finally:
            if robot is self.robot:
                self.drop_lease()
                self.robot = None
                self.task = None
                self.connected = False
                self.safety = None
                self.close_camera()
                self.refresh()

    def show_frame(self, packet: EventFrame) -> None:
        previous = self.current
        self.current = packet.run
        self.safety = packet.safety
        run_id = packet.run.id if packet.run else None
        if self.leased_run is not None and (
            (packet.safety is not None and packet.safety.latched)
            or (
                packet.run is not None
                and run_id == self.leased_run
                and packet.run.status != "running"
            )
            or (previous is not None and previous.id == self.leased_run and run_id != previous.id)
        ):
            self.drop_lease()
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
        if self.closed or not self.page_available:
            return
        watching = self.task is not None
        current = self.current
        busy = current is not None and current.status in ("running", "stopping")
        hardware = self.robot is not None and self.robot.kind == "yahboom"
        safety_pending = hardware and self.safety_pending is self.robot
        safety = self.safety
        self.address.disabled = watching
        self.token.disabled = watching
        self.connection_button.content = "Отключиться" if watching else "Подключиться"
        self.connection_status.color = ACCENT if self.connected else MUTED
        self.camera_button.visible = bool(
            self.connected and self.robot is not None and self.robot.camera_available
        )
        self.camera_button.disabled = self.camera is not None
        self.selector.disabled = not self.connected or busy or self.pending
        selected = self.scripts.get(self.selector.value or "")
        self.description.value = selected.description if selected else ""
        self.start_button.disabled = (
            self.selector.disabled
            or selected is None
            or self.heartbeat_task is not None
            or (
                selected.requires_heartbeat and (safety_pending or safety is None or safety.latched)
            )
        )
        self.parameters_button.disabled = (
            self.start_button.disabled or selected is None or not selected.parameters
        )
        self.stop_button.disabled = (
            not self.connected or self.pending or current is None or current.status != "running"
        )
        self.safety_controls.visible = hardware
        self.emergency_button.disabled = not hardware or safety_pending
        self.reset_button.disabled = (
            not hardware
            or not self.connected
            or safety_pending
            or busy
            or self.pending
            or self.heartbeat_task is not None
            or safety is None
            or not safety.latched
        )
        self.safety_status.value = self.format_safety(safety)
        self.safety_status.color = ERROR if safety and safety.latched else MUTED
        self.empty_log.visible = not self.log.controls
        self.run_status.value = self.format_run(current)
        self.page.update()

    @staticmethod
    def format_safety(safety: SafetyState | None) -> str:
        if safety is None:
            return "Состояние блокировки неизвестно."
        fields = [
            "Аппаратные запуски заблокированы." if safety.latched else "Аварийная блокировка снята."
        ]
        if safety.motor_disable_confirmed is True:
            fields.append("ENA и ENB: LOW (проверка GPIO).")
        elif safety.latched:
            fields.append(
                "Проверка GPIO не подтвердила LOW на ENA и ENB."
                if safety.motor_disable_confirmed is False
                else "Программное отключение двигателей пока не подтверждено."
            )
        if safety.reason:
            fields.append(safety.reason)
        return " ".join(fields)

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

    def configuration_key(self, robot: RobotConnection, script: Script) -> str:
        return f"robolab.configuration.v1.{robot.url}.{script.id}"

    async def configure(self, event: ft.ControlEvent) -> None:
        robot = self.robot
        script = self.scripts.get(self.selector.value or "")
        if robot is None or script is None or self.parameters_button.disabled:
            return
        key = self.configuration_key(robot, script)
        self.pending = True
        self.set_error("")
        self.refresh()
        try:
            parameters, presets = await asyncio.gather(
                self.configurations.current(key, script), self.configurations.preset_names(key)
            )
        except (RuntimeError, TimeoutError) as exc:
            self.set_error(str(exc) or "Не удалось прочитать настройки.")
            return
        finally:
            self.pending = False
            self.refresh()
        if robot is self.robot and not self.parameters_button.disabled:
            ParameterDialog(self.page, script, key, self.configurations, parameters, presets).show()

    async def start(self, event: ft.ControlEvent) -> None:
        robot = self.robot
        script = self.scripts.get(self.selector.value or "")
        if self.closed or not self.page_available or robot is None or script is None:
            return
        if self.start_button.disabled:
            return
        generation = self.lease_generation

        async def launch() -> None:
            parameters = {}
            if script.parameters:
                try:
                    parameters = await self.configurations.current(
                        self.configuration_key(robot, script), script
                    )
                except (RuntimeError, TimeoutError) as exc:
                    raise RobotError(str(exc) or "Не удалось прочитать настройки.") from exc
            if robot is not self.robot or generation != self.lease_generation:
                return
            sent_at = asyncio.get_running_loop().time()
            started = await robot.start(
                script.id, parameters, requires_heartbeat=script.requires_heartbeat
            )
            if (
                robot is not self.robot
                or generation != self.lease_generation
                or not script.requires_heartbeat
                or started.status != "running"
                or started.lease is None
                or self.safety is None
                or self.safety.latched
            ):
                return
            current = self.current
            if current is not None and (
                (current.id == started.id and current.status != "running")
                or current.started_at > started.started_at
            ):
                return
            self.leased_run = started.id
            self.heartbeat_task = asyncio.create_task(
                self.keep_lease(robot, started.id, started.lease, sent_at + LEASE_TIMEOUT)
            )

        await self.command(launch)

    async def keep_lease(
        self, robot: RobotConnection, run_id: UUID, lease: UUID, expires_at: float
    ) -> None:
        loop = asyncio.get_running_loop()
        try:
            while (sent_at := loop.time()) < expires_at:
                try:
                    async with asyncio.timeout(min(HEARTBEAT_PERIOD, expires_at - sent_at)):
                        await robot.heartbeat(run_id, lease)
                except TimeoutError:
                    pass
                except RobotError as exc:
                    if not exc.retryable:
                        return
                else:
                    if loop.time() >= expires_at:
                        return
                    expires_at = sent_at + LEASE_TIMEOUT
                next_send = min(sent_at + HEARTBEAT_PERIOD, expires_at)
                await asyncio.sleep(max(0, next_send - loop.time()))
        finally:
            if self.heartbeat_task is asyncio.current_task():
                self.heartbeat_task = None
                self.leased_run = None
                self.refresh()

    def drop_lease(self) -> None:
        self.lease_generation += 1
        task, self.heartbeat_task = self.heartbeat_task, None
        self.leased_run = None
        if task is not None:
            task.cancel()

    async def stop(self, event: ft.ControlEvent) -> None:
        robot, current = self.robot, self.current
        if robot and current and not self.stop_button.disabled:
            if current.id == self.leased_run:
                self.drop_lease()
            await self.command(lambda: robot.stop(current.id))

    async def emergency_stop(self, event: ft.ControlEvent) -> None:
        robot = self.robot
        if robot is not None and not self.emergency_button.disabled:
            self.drop_lease()
            await self.safety_command(robot, robot.safety_stop)

    async def reset_safety(self, event: ft.ControlEvent) -> None:
        robot = self.robot
        if robot is not None and not self.reset_button.disabled:
            await self.safety_command(robot, robot.safety_reset)

    async def safety_command(
        self, robot: RobotConnection, action: Callable[[], Awaitable[SafetyState]]
    ) -> None:
        self.safety_pending = robot
        self.set_error("")
        self.refresh()
        state = None
        try:
            state = await action()
        except RobotError as exc:
            if robot is self.robot:
                self.set_error(str(exc))
        finally:
            if self.safety_pending is robot:
                self.safety_pending = None
            if robot is self.robot:
                self.safety = state
                self.event_generation += 1
                self.refresh()

    async def command(self, action: Callable[[], Awaitable[None]]) -> None:
        robot = self.robot
        self.pending = True
        self.set_error("")
        self.refresh()
        try:
            await action()
        except RobotError as exc:
            if robot is self.robot:
                self.set_error(str(exc))
        finally:
            self.pending = False
            self.refresh()

    async def reconnect_page(self, event: ft.ControlEvent) -> None:
        self.page_available = True
        self.refresh()

    async def disconnect_page(self, event: ft.ControlEvent) -> None:
        self.page_available = False
        self.drop_lease()
        self.close_camera()

    async def close(self, event: ft.ControlEvent) -> None:
        self.closed = True
        camera_task = self.camera.task if self.camera is not None else None
        await self.disconnect()
        if camera_task is not None:
            with suppress(asyncio.CancelledError):
                await camera_task

    async def open_camera(self, event: ft.ControlEvent) -> None:
        robot = self.robot
        if (
            self.closed
            or not self.page_available
            or not self.connected
            or robot is None
            or not robot.camera_available
            or self.camera is not None
        ):
            return
        self.camera = CameraDialog(self.page, robot, self.camera_picker, self.camera_closed)
        self.camera.show()
        self.refresh()

    def close_camera(self) -> None:
        if self.camera is not None:
            self.camera.close(update=self.page_available and not self.closed)

    def camera_closed(self, camera: CameraDialog) -> None:
        if self.camera is camera:
            self.camera = None
            self.refresh()


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
