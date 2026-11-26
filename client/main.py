import argparse
import asyncio
from collections.abc import Awaitable, Callable
from contextlib import aclosing, suppress
from uuid import UUID

import flet as ft

from client.appearance import (
    AMBER,
    AMBER_TINT,
    BACKGROUND,
    CONTROL_RADIUS,
    DANGER,
    ERROR,
    ERROR_TINT,
    LINE,
    MUTED,
    PRIMARY,
    PRIMARY_TINT,
    SUCCESS,
    SURFACE,
    TEAL,
    TEAL_TINT,
    TEXT,
    button_row,
    light_theme,
)
from client.camera import CameraPanel
from client.configuration import ConfigurationStore, ParameterDialog
from client.connection import RobotConnection, RobotError
from client.experiments import RUN_STATUS, ExperimentsPanel, TelemetryPanel
from client.files import FilePanel
from client.manual import ManualPanel
from client.run_journal import RunJournal
from client.ssh import PoweroffUnconfirmed, SshError, SshSession, SshTarget
from client.ssh_login import SshLogin, confirm, safe_poweroff
from common.protocol import MANUAL_SCRIPT_ID, EventFrame, RunState, SafetyState, Script

HEARTBEAT_PERIOD = 0.7
LEASE_TIMEOUT = 4


class Dashboard:
    def __init__(self, page: ft.Page) -> None:
        self.page = page
        self.task: asyncio.Task[None] | None = None
        self.heartbeat_task: asyncio.Task[None] | None = None
        self.robot: RobotConnection | None = None
        self.ssh_session: SshSession | None = None
        self.ssh_target: SshTarget | None = None
        self.ssh_task: asyncio.Task[None] | None = None
        self.ssh_monitor: asyncio.Task[None] | None = None
        self.agent_task: asyncio.Task[None] | None = None
        self.poweroff_pending = False
        self.poweroff_dialog = False
        self.disconnecting = False
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
        self.camera: CameraPanel | None = None
        self.camera_hold: UUID | None = None
        self.camera_picker = ft.FilePicker()
        self.observability: TelemetryPanel | ExperimentsPanel | None = None
        self.files: FilePanel | None = None
        self.files_open = False
        self.compact = False

        self.address = ft.TextField(label="Адрес агента", value="http://127.0.0.1:8765")
        self.token = ft.TextField(label="Токен агента", password=True, can_reveal_password=True)
        self.ssh_login = None if page.web else SshLogin(page)
        self.connection_mode = ft.Dropdown(
            label="Способ подключения",
            value="ssh" if self.ssh_login else "agent",
            options=[
                ft.DropdownOption(key="ssh", text="SSH к Raspberry Pi"),
                ft.DropdownOption(key="agent", text="Напрямую к агенту"),
            ],
            visible=self.ssh_login is not None,
            on_select=self.select_connection,
        )
        self.connection_button = ft.FilledButton(
            "Подключиться",
            on_click=self.toggle_connection,
            height=46,
        )
        self.exit_button = ft.TextButton("Выйти", on_click=self.exit)
        self.poweroff_button = ft.OutlinedButton(
            "Выключить Pi",
            icon=ft.Icons.POWER_SETTINGS_NEW,
            height=44,
            on_click=self.request_poweroff,
            visible=False,
        )
        self.connection_status = ft.Text("Нет подключения", color=MUTED)
        self.telemetry_button = ft.OutlinedButton(
            "Телеметрия", visible=False, on_click=self.open_telemetry
        )
        self.experiments_button = ft.OutlinedButton(
            "Эксперименты", visible=False, on_click=self.open_experiments
        )
        self.files_button = self.action_button(
            "Файлы",
            "Открыть домашнюю папку",
            ft.Icons.FOLDER_OUTLINED,
            self.open_files,
            primary=True,
        )
        self.agent_button = self.action_button(
            "Подготовить эксперименты",
            "RoboLab запустит агент по SSH",
            ft.Icons.PLAY_ARROW_OUTLINED,
            self.prepare_agent,
        )
        self.observability_content = ft.Column(horizontal_alignment=ft.CrossAxisAlignment.STRETCH)
        self.observability_card = ft.Container(
            expand=True,
            alignment=ft.Alignment.TOP_CENTER,
            content=ft.Column(
                [self.observability_content],
                scroll=ft.ScrollMode.AUTO,
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                expand=True,
            ),
        )
        self.observability_card.visible = False
        self.selector = ft.Dropdown(label="Скрипт", disabled=True, on_select=self.select_script)
        self.description = ft.Text(color=MUTED, size=13)
        self.parameters_button = ft.OutlinedButton(
            "Параметры", height=44, disabled=True, on_click=self.configure
        )
        self.start_button = ft.FilledButton(
            "Запустить", height=44, disabled=True, on_click=self.start
        )
        self.stop_button = ft.OutlinedButton(
            "Остановить", height=44, disabled=True, on_click=self.stop
        )
        self.run_status = ft.Text("Запусков пока нет.", color=MUTED, size=13)
        self.run_status_box = ft.Container(
            self.run_status,
            padding=ft.Padding.symmetric(horizontal=11, vertical=7),
            border_radius=CONTROL_RADIUS,
            bgcolor=PRIMARY_TINT,
        )
        self.emergency_button = ft.FilledButton(
            "Аварийная остановка",
            height=44,
            style=ft.ButtonStyle(
                bgcolor=DANGER,
                color=SURFACE,
                shape=ft.RoundedRectangleBorder(radius=CONTROL_RADIUS),
            ),
            disabled=True,
            on_click=self.emergency_stop,
        )
        self.reset_button = ft.OutlinedButton(
            "Снять блокировку", height=44, disabled=True, on_click=self.reset_safety
        )
        self.safety_status = ft.Text(color=MUTED)
        self.safety_controls = ft.Column(
            [self.safety_status, button_row(self.reset_button, self.poweroff_button)],
            visible=False,
            spacing=8,
        )
        self.error = ft.Text(color=ERROR, visible=False)
        self.journal = RunJournal()
        self.manual = ManualPanel(page, self.manual_robot, self.refresh)

    def mount(self) -> None:
        self.page.title = "RoboLab"
        self.page.theme_mode = ft.ThemeMode.LIGHT
        self.page.theme = light_theme()
        self.page.bgcolor = BACKGROUND
        self.page.padding = 0
        self.page.on_connect = self.reconnect_page
        self.page.on_disconnect = self.disconnect_page
        self.page.on_close = self.close
        self.page.on_resize = self.resize
        self.page.on_app_lifecycle_state_change = self.manual_lifecycle
        self.page.services.append(self.camera_picker)

        self.desktop_status = ft.Text(size=15, color=MUTED)
        self.mobile_status = ft.Text(size=13, color=MUTED)
        self.desktop_dot = self.status_dot()
        self.mobile_dot = self.status_dot()
        self.desktop_identity = ft.Row(
            [
                ft.Container(height=29, width=1, bgcolor=LINE, margin=ft.Margin.only(right=4)),
                self.desktop_dot,
                ft.Container(self.desktop_status, expand=True),
            ],
            spacing=9,
            expand=True,
        )
        self.mobile_identity = ft.Row([self.mobile_dot, self.mobile_status], spacing=8)
        self.brand = self.heading("RoboLab", 30)
        self.header = ft.Container(
            content=ft.Column(
                [
                    ft.Row(
                        [
                            self.brand,
                            self.desktop_identity,
                            self.exit_button,
                        ],
                        alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                        spacing=20,
                    ),
                    self.mobile_identity,
                ],
                spacing=3,
            ),
            border=ft.Border.only(bottom=ft.BorderSide(1, LINE)),
        )
        self.emergency_strip = ft.Container(
            self.emergency_button, alignment=ft.Alignment.CENTER_RIGHT, visible=False
        )

        controls = (
            ("run", "Работа", ft.Icons.PLAY_ARROW_OUTLINED, self.show_run),
            ("files", "Файлы", ft.Icons.FOLDER_OUTLINED, self.open_files),
            ("telemetry", "Телеметрия", ft.Icons.QUERY_STATS, self.open_telemetry),
            ("experiments", "Эксперименты", ft.Icons.SCIENCE_OUTLINED, self.open_experiments),
        )
        self.nav = {
            key: self.tab(label, icon, click, mobile=False) for key, label, icon, click in controls
        }
        self.mobile_nav = {
            key: self.tab("История" if key == "experiments" else label, icon, click, mobile=True)
            for key, label, icon, click in controls
        }
        self.desktop_navigation = ft.Container(
            ft.Row(list(self.nav.values()), spacing=0, scroll=ft.ScrollMode.AUTO),
            border=ft.Border.only(bottom=ft.BorderSide(1, LINE)),
        )
        self.mobile_navigation = ft.Container(
            ft.Row(list(self.mobile_nav.values()), spacing=0),
            border=ft.Border.only(top=ft.BorderSide(1, LINE)),
        )

        self.connection_view = ft.Container(
            alignment=ft.Alignment.TOP_CENTER,
            content=ft.Column(
                [
                    self.heading("Подключение к роботу", 32),
                    self.connection_mode,
                    *([self.ssh_login.control] if self.ssh_login is not None else []),
                    self.address,
                    self.token,
                    self.connection_button,
                ],
                width=520,
                spacing=13,
                horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
            ),
        )
        self.workspace_title = self.heading("Работа с роботом", 40)
        self.workspace_home = ft.Column(
            [self.workspace_title, self.files_button, self.agent_button],
            spacing=26,
            horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
            visible=False,
        )
        self.run_title = self.heading("Работа с роботом", 32)
        self.run_controls = ft.Column(
            [
                self.run_title,
                self.selector,
                self.description,
                button_row(self.parameters_button, self.start_button, self.stop_button),
                self.run_status_box,
                self.manual.content,
            ],
            spacing=8,
            horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
            visible=False,
        )
        self.camera_slot = ft.Container(content=None, col={"xs": 12, "md": 6}, visible=False)
        self.journal_slot = ft.Container(
            self.journal.content,
            col=12,
            padding=12,
            bgcolor=SURFACE,
            border=ft.Border.all(1, LINE),
            border_radius=CONTROL_RADIUS,
        )
        self.monitor_grid = ft.ResponsiveRow(
            [self.camera_slot, self.journal_slot],
            spacing=14,
            run_spacing=10,
            vertical_alignment=ft.CrossAxisAlignment.START,
            visible=False,
        )
        self.run_body = ft.Container(
            content=ft.Column(
                [
                    self.connection_view,
                    self.workspace_home,
                    self.run_controls,
                    self.monitor_grid,
                    self.safety_controls,
                ],
                spacing=16,
                horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
            )
        )
        self.run_view = ft.Container(
            expand=True,
            content=ft.Column(
                [self.run_body],
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                scroll=ft.ScrollMode.AUTO,
            ),
        )
        self.files_view = ft.Container(
            expand=True, visible=False, alignment=ft.Alignment.TOP_CENTER
        )
        self.error_banner = ft.Container(
            self.error,
            bgcolor=ERROR_TINT,
            border_radius=CONTROL_RADIUS,
            visible=False,
            padding=ft.Padding.symmetric(horizontal=13, vertical=9),
        )
        self.files_safety = ft.Text(size=14, color=ERROR, visible=False)
        self.layout()
        self.page.add(
            ft.SafeArea(
                expand=True,
                content=ft.Column(
                    [
                        self.header,
                        self.desktop_navigation,
                        self.emergency_strip,
                        self.error_banner,
                        self.files_safety,
                        self.run_view,
                        self.observability_card,
                        self.files_view,
                        self.mobile_navigation,
                    ],
                    spacing=0,
                    horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
                ),
            )
        )
        self.refresh()
        if self.ssh_login is not None:
            self.page.run_task(self.restore_login)

    @staticmethod
    def heading(text: str, size: int) -> ft.Text:
        return ft.Text(text, size=size, color=TEXT, weight=ft.FontWeight.W_800)

    @staticmethod
    def status_dot() -> ft.Container:
        return ft.Container(width=9, height=9, bgcolor=MUTED, border_radius=5)

    @staticmethod
    def action_button(
        title: str,
        detail: str,
        icon: ft.IconData,
        handler: Callable[[ft.ControlEvent], Awaitable[None]],
        *,
        primary: bool = False,
    ) -> ft.Button:
        color = SURFACE if primary else TEXT
        return ft.Button(
            content=ft.Row(
                [
                    ft.Icon(icon, size=32, color=color),
                    ft.Column(
                        [
                            ft.Text(title, size=22, color=color, weight=ft.FontWeight.W_700),
                            ft.Text(detail, size=15, color=color if primary else MUTED),
                        ],
                        spacing=3,
                        expand=True,
                        alignment=ft.MainAxisAlignment.CENTER,
                    ),
                    ft.Icon(ft.Icons.CHEVRON_RIGHT, size=25, color=color),
                ],
                spacing=17,
            ),
            style=ft.ButtonStyle(
                bgcolor=PRIMARY if primary else TEAL_TINT,
                color=color,
                shape=ft.RoundedRectangleBorder(radius=CONTROL_RADIUS),
                padding=ft.Padding.symmetric(horizontal=21, vertical=13),
                elevation=0,
            ),
            height=96,
            on_click=handler,
            visible=False,
        )

    @staticmethod
    def tab(
        label: str,
        icon: ft.IconData,
        action: Callable[[ft.ControlEvent], Awaitable[None]],
        *,
        mobile: bool,
    ) -> ft.Container:
        if mobile:
            content = ft.Column(
                [
                    ft.Icon(icon, size=20),
                    ft.Text(
                        label,
                        size=12,
                        weight=ft.FontWeight.W_600,
                        max_lines=1,
                        overflow=ft.TextOverflow.ELLIPSIS,
                    ),
                ],
                spacing=2,
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                alignment=ft.MainAxisAlignment.CENTER,
            )
        else:
            content = ft.Text(label, size=15, weight=ft.FontWeight.W_700)
        return ft.Container(
            ft.TextButton(
                content, tooltip=label, on_click=action, expand=True, height=64 if mobile else 51
            ),
            expand=mobile,
        )

    def layout(self) -> None:
        width = self.page.width or 1024
        self.compact = width < 640
        inset = 16 if self.compact else 34
        self.header.padding = ft.Padding.symmetric(
            horizontal=inset, vertical=12 if self.compact else 20
        )
        self.desktop_identity.visible = not self.compact
        self.mobile_identity.visible = self.compact
        self.brand.size = 24 if self.compact else 30
        self.desktop_navigation.padding = ft.Padding.symmetric(horizontal=inset)
        self.run_view.padding = ft.Padding.only(
            left=inset, right=inset, top=32 if self.compact else 62, bottom=24
        )
        self.run_body.width = min(max(width - inset * 2, 1), 1110)
        self.selector.width = self.run_body.width if self.compact else 380
        self.connection_view.content.width = min(self.run_body.width, 520)
        self.connection_mode.width = self.connection_view.content.width
        self.workspace_title.size = 29 if self.compact else 46
        self.workspace_home.spacing = 20 if self.compact else 30
        self.observability_content.width = min(max(width - inset * 2, 1), 1110)
        self.observability_card.padding = ft.Padding.symmetric(horizontal=inset, vertical=20)
        self.error_banner.margin = ft.Margin.only(left=inset, right=inset, top=8)
        self.emergency_strip.padding = ft.Padding.only(right=inset, left=inset, top=8)
        self.emergency_button.width = max(width - inset * 2, 1) if self.compact else None
        self.files_view.padding = ft.Padding.symmetric(horizontal=inset, vertical=12)
        self.files_button.height = self.agent_button.height = 99 if self.compact else 136
        self.journal.set_compact(self.compact)
        if self.camera is not None:
            self.camera.set_compact(self.compact)
        if self.files is not None:
            self.files.set_compact(self.compact)

    async def resize(self, event: ft.ControlEvent) -> None:
        self.layout()
        self.refresh()

    async def restore_login(self) -> None:
        login = self.ssh_login
        if login is not None:
            await login.restore()
            self.refresh()

    async def select_connection(self, event: ft.ControlEvent) -> None:
        self.set_error("")
        self.refresh()

    async def exit(self, event: ft.ControlEvent) -> None:
        if not self.poweroff_pending:
            await self.disconnect()

    async def toggle_connection(self, event: ft.ControlEvent) -> None:
        if self.closed or not self.page_available or self.connection_button.disabled:
            return
        if self.task is not None or self.ssh_task is not None or self.ssh_session is not None:
            return
        self.set_error("")
        if self.ssh_login is not None and self.connection_mode.value == "ssh":
            self.ssh_login.message("")
            self.ssh_task = asyncio.create_task(self.connect_ssh(self.ssh_login))
            self.connection_status.value = "Подключение по SSH…"
            self.refresh()
            return
        try:
            robot = RobotConnection(self.address.value or "", (self.token.value or "").strip())
        except ValueError as exc:
            self.set_error(str(exc))
            self.refresh()
            return
        self.connect_agent(robot)

    async def connect_ssh(self, login: SshLogin) -> None:
        session = None
        try:
            target, session = await login.connect()
            if self.closed or not self.page_available:
                return
            self.ssh_target, self.ssh_session = target, session
            self.ssh_monitor = asyncio.create_task(self.monitor_ssh(session))
            self.connection_status.value = "SSH · подключено"
            session = None
        except (SshError, ValueError) as exc:
            self.connection_status.value = "Нет подключения"
            self.set_error(str(exc))
        except (RuntimeError, TimeoutError):
            self.connection_status.value = "Нет подключения"
            self.set_error("Не удалось прочитать или сохранить параметры SSH.")
        finally:
            if self.ssh_task is asyncio.current_task():
                self.ssh_task = None
            if session is not None:
                await session.close()
            self.refresh()

    async def monitor_ssh(self, session: SshSession) -> None:
        await session.wait_closed()
        if self.ssh_session is session and not self.poweroff_pending:
            await self.disconnect()
            self.connection_status.value = "SSH-соединение закрыто"
            self.refresh()

    async def prepare_agent(self, event: ft.ControlEvent) -> None:
        session = self.ssh_session
        if session is None or session.closed or self.agent_button.disabled:
            return
        self.agent_task = asyncio.current_task()
        self.refresh()
        try:
            folder = ft.TextField(
                label="Папка со скриптом",
                hint_text="/путь/на/роботе",
            )
            accepted = await confirm(
                self.page,
                "Подготовить эксперименты",
                ft.Column(
                    [
                        ft.Text("RoboLab сам подготовит и запустит агент по SSH."),
                        ft.Text(
                            "Для работы с роботом укажите папку со скриптом и его настройками. "
                            "Оставьте поле пустым, чтобы использовать готовую конфигурацию."
                        ),
                        folder,
                    ],
                    tight=True,
                ),
                "Подготовить",
            )
            if not accepted or self.ssh_session is not session or session.closed:
                return
            body = (folder.value or "").strip()
            self.set_error("")
            self.connection_status.value = "SSH · подготовка агента…"
            self.refresh()
            address, token = await session.prepare_agent(body or None)
            robot = RobotConnection(address, token)
            await robot.catalog()
            if self.ssh_session is session:
                self.connect_agent(robot)
        except (RobotError, SshError, ValueError) as exc:
            if self.ssh_session is session and not session.closed:
                self.connection_status.value = "SSH · подключено"
                self.set_error(str(exc))
        finally:
            try:
                if self.robot is None:
                    await session.close_agent()
            finally:
                if self.agent_task is asyncio.current_task():
                    self.agent_task = None
                self.refresh()

    def connect_agent(self, robot: RobotConnection) -> None:
        self.robot = robot
        self.scripts = {}
        self.selector.options = []
        self.selector.value = None
        self.current, self.run_id, self.cursor = None, None, 0
        self.camera_hold = None
        self.journal.reset()
        self.connection_status.value = "Подключение…"
        self.task = asyncio.create_task(self.watch(robot))
        self.refresh()

    async def disconnect(self) -> None:
        self.disconnecting = True
        await self.manual.deactivate()
        task, self.task = self.task, None
        ssh_task, self.ssh_task = self.ssh_task, None
        agent_task, self.agent_task = self.agent_task, None
        monitor, self.ssh_monitor = self.ssh_monitor, None
        session, self.ssh_session, self.ssh_target = self.ssh_session, None, None
        self.drop_lease()
        self.robot = None
        self.connected = False
        self.safety = None
        self.camera_hold = None
        self.token.value = ""
        if self.ssh_login is not None:
            self.ssh_login.password.value = ""
        self.set_error("")
        self.connection_status.value = "Отключено"
        observability_task = self.close_observability()
        files_task = self.close_files()
        camera_task = self.close_camera()
        tasks = [
            pending
            for pending in (task, ssh_task, agent_task, monitor)
            if pending is not None and pending is not asyncio.current_task()
        ]
        for pending in tasks:
            pending.cancel()
        self.refresh()
        try:
            for pending in (*tasks, observability_task, files_task, camera_task):
                if pending is not None:
                    with suppress(asyncio.CancelledError):
                        await pending
        finally:
            try:
                if session is not None:
                    await session.close()
            finally:
                self.disconnecting = False
                self.refresh()

    async def watch(self, robot: RobotConnection) -> None:
        delay = 1
        try:
            while True:
                try:
                    scripts = await robot.catalog()
                    if not robot.camera_available:
                        self.close_camera()
                    if not robot.observability_available:
                        self.close_observability()
                    if self.ssh_session is None and not robot.files_available:
                        self.close_files()
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
                    await self.manual.deactivate()
                    self.close_observability()
                    if self.ssh_session is None:
                        self.close_files()
                    self.close_camera()
                    if robot.kind == "yahboom":
                        self.drop_lease()
                        self.safety = None
                        self.camera_hold = None
                    if self.ssh_session is not None and self.ssh_session.closed:
                        self.connection_status.value = "SSH-соединение закрыто"
                        self.set_error("Подключитесь заново.")
                        return
                    if not exc.retryable or (
                        self.ssh_session is not None and self.ssh_session.manages_agent
                    ):
                        self.connection_status.value = (
                            "SSH · подключено" if self.ssh_session else "Нет подключения"
                        )
                        self.set_error(str(exc))
                        return
                    self.connection_status.value = f"{exc} Повтор через {delay} с"
                    self.refresh()
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 8)
        finally:
            if robot is self.robot:
                await self.manual.deactivate()
                self.drop_lease()
                self.robot = None
                self.connected = False
                self.safety = None
                self.close_observability()
                if self.ssh_session is None:
                    self.close_files()
                camera_task = self.close_camera()
                session = self.ssh_session
                try:
                    if camera_task is not None:
                        with suppress(asyncio.CancelledError):
                            await camera_task
                    if session is not None:
                        await session.close_agent()
                finally:
                    if self.task is asyncio.current_task():
                        self.task = None
                    self.refresh()

    def show_frame(self, packet: EventFrame) -> None:
        previous = self.current
        previous_safety = self.safety
        self.current = packet.run
        self.safety = packet.safety
        self.manual.observe(packet.run)
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
            self.journal.reset()
        if run_id == self.camera_hold:
            self.camera_hold = None
        starting = packet.run is not None and packet.run.status == "running"
        if starting:
            self.journal.record_run(previous, packet.run)
        if packet.gap:
            self.journal.gap.visible = True
        for line in packet.logs:
            if line.sequence > self.cursor:
                self.journal.record_line(line)
                self.cursor = line.sequence
        if not starting:
            self.journal.record_run(previous, packet.run)
        self.journal.record_safety(previous_safety, packet.safety)

    def refresh(self) -> None:
        if self.closed or not self.page_available:
            return
        live_ssh = self.ssh_session is not None and not self.ssh_session.closed
        watching = self.task is not None or self.ssh_task is not None or live_ssh
        current = self.current
        via_ssh = self.ssh_login is not None and self.connection_mode.value == "ssh"
        busy = current is not None and current.status in ("running", "stopping")
        hardware = self.robot is not None and self.robot.kind == "yahboom"
        safety_pending = hardware and self.safety_pending is self.robot
        safety = self.safety
        self.address.visible = self.token.visible = not via_ssh
        self.address.disabled = self.token.disabled = watching or self.disconnecting
        self.connection_mode.disabled = watching or self.disconnecting
        if self.ssh_login is not None:
            self.ssh_login.control.visible = via_ssh
            self.ssh_login.refresh(watching or self.disconnecting)
        self.connection_button.visible = not watching
        self.connection_button.disabled = (
            self.disconnecting
            or self.poweroff_pending
            or bool(
                via_ssh and self.ssh_login and (self.ssh_login.loading or self.ssh_login.forgetting)
            )
        )
        self.exit_button.visible = watching
        self.exit_button.disabled = self.poweroff_pending
        self.poweroff_button.visible = bool(hardware and self.connected and self.ssh_session)
        self.poweroff_button.disabled = bool(
            self.poweroff_pending or self.poweroff_dialog or self.safety_pending or self.pending
        )
        online = self.connected or live_ssh
        display_status = self.connection_status.value
        if display_status == "SSH · подключено":
            display_status = "Raspberry Pi · SSH подключён"
        self.desktop_status.value = self.mobile_status.value = display_status
        self.desktop_dot.bgcolor = self.mobile_dot.bgcolor = SUCCESS if online else MUTED
        self.refresh_camera(hardware and (busy or self.pending or self.camera_hold is not None))
        self.telemetry_button.visible = self.experiments_button.visible = bool(
            self.connected and self.robot is not None and self.robot.observability_available
        )
        self.telemetry_button.disabled = isinstance(self.observability, TelemetryPanel)
        self.experiments_button.disabled = isinstance(self.observability, ExperimentsPanel)
        self.files_button.visible = live_ssh or bool(
            self.connected and self.robot is not None and self.robot.files_available
        )
        self.agent_button.visible = live_ssh and self.task is None and self.robot is None
        self.agent_button.disabled = (
            not self.agent_button.visible or self.agent_task is not None or self.disconnecting
        )
        self.connection_view.visible = not live_ssh and self.robot is None
        self.workspace_home.visible = live_ssh and self.robot is None
        self.run_controls.visible = self.robot is not None
        self.monitor_grid.visible = self.run_controls.visible
        if self.run_controls.visible:
            self.run_view.padding.top = 18 if self.compact else 24
        else:
            self.run_view.padding.top = 32 if self.compact else 62
        self.error_banner.visible = self.error.visible
        self.run_view.visible = not self.files_open and self.observability is None
        self.files_view.visible = self.files_open
        self.refresh_navigation()
        self.selector.disabled = not self.connected or busy or self.pending or self.poweroff_dialog
        selected = self.scripts.get(self.selector.value or "")
        compact_run = self.compact and busy and self.camera_slot.visible
        self.selector.visible = self.description.visible = not compact_run
        self.run_title.value = (
            self.scripts[current.script_id].name
            if compact_run and current is not None and current.script_id in self.scripts
            else "Работа с роботом"
        )
        self.run_title.size = 23 if compact_run else 30 if self.compact else 32
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
        self.manual.update(bool(self.robot and getattr(self.robot, "manual_available", False)))
        self.safety_controls.visible = hardware
        self.emergency_button.visible = hardware
        self.emergency_strip.visible = hardware
        self.emergency_button.disabled = not hardware or safety_pending
        self.reset_button.disabled = (
            not hardware
            or not self.connected
            or safety_pending
            or self.poweroff_dialog
            or busy
            or self.pending
            or self.heartbeat_task is not None
            or safety is None
            or not safety.latched
        )
        self.safety_status.value = self.format_safety(safety)
        self.safety_status.color = ERROR if safety and safety.latched else MUTED
        self.files_safety.value = self.safety_status.value
        self.files_safety.visible = bool(self.files_open and safety and safety.latched)
        self.run_status.value = self.format_run(current)
        colors = {
            "running": (TEAL, TEAL_TINT),
            "stopping": (AMBER, AMBER_TINT),
            "stopped": (AMBER, AMBER_TINT),
            "failed": (ERROR, ERROR_TINT),
        }
        self.run_status.color, self.run_status_box.bgcolor = colors.get(
            current.status if current else "", (MUTED, PRIMARY_TINT)
        )
        self.page.update()

    def refresh_navigation(self) -> None:
        online = self.connected or (self.ssh_session is not None and not self.ssh_session.closed)
        self.desktop_navigation.visible = online and not self.compact
        self.mobile_navigation.visible = online and self.compact
        for name, button in (
            ("telemetry", self.telemetry_button),
            ("experiments", self.experiments_button),
            ("files", self.files_button),
        ):
            self.nav[name].visible = self.mobile_nav[name].visible = button.visible
        selected = "run"
        if self.files_open:
            selected = "files"
        elif isinstance(self.observability, TelemetryPanel):
            selected = "telemetry"
        elif isinstance(self.observability, ExperimentsPanel):
            selected = "experiments"
        for mobile, navigation in ((False, self.nav), (True, self.mobile_nav)):
            compact_labels = sum(tab.visible for tab in navigation.values()) > 3
            for name, tab in navigation.items():
                is_selected = name == selected
                side = ft.BorderSide(3, PRIMARY if is_selected else BACKGROUND)
                tab.border = ft.Border.only(top=side) if mobile else ft.Border.only(bottom=side)
                tab.content.style = ft.ButtonStyle(
                    padding=ft.Padding.symmetric(horizontal=3 if mobile else 20),
                    shape=ft.RoundedRectangleBorder(radius=2),
                    color=PRIMARY if is_selected else MUTED,
                )
                if mobile:
                    tab.content.content.controls[1].size = 10 if compact_labels else 12

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
        fields = ["Ручное управление"] if current.script_id == MANUAL_SCRIPT_ID else []
        fields.extend([RUN_STATUS[current.status], f"{current.elapsed_seconds:.1f} с"])
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
        target = self.ssh_target
        origin = f"ssh://{target.username}@{target.host}:{target.port}" if target else robot.url
        return f"robolab.configuration.v1.{origin}.{script.id}"

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
        if self.manual.voice_requested:
            await self.manual.deactivate()
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
            camera = self.camera
            if (
                script.requires_heartbeat
                and camera is not None
                and robot is self.robot
                and generation == self.lease_generation
            ):
                await camera.pause_for_run()
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
            if current is None or current.id != started.id:
                self.camera_hold = started.id
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
            if self.manual.owned is not None and self.manual.owned[1].id == current.id:
                await self.manual.command("stop")
                return
            if current.id == self.leased_run:
                self.drop_lease()
            await self.command(lambda: robot.stop(current.id))

    async def emergency_stop(self, event: ft.ControlEvent) -> None:
        robot = self.robot
        if robot is not None and not self.emergency_button.disabled:
            self.drop_lease()
            self.manual.cancel_heartbeat()
            await asyncio.gather(
                self.safety_command(robot, robot.safety_stop), self.manual.deactivate()
            )

    async def request_poweroff(self, event: ft.ControlEvent) -> None:
        robot, session, target = self.robot, self.ssh_session, self.ssh_target
        if robot is None or session is None or target is None or self.poweroff_button.disabled:
            return
        if not self.poweroff_button.visible:
            return
        if self.files is not None and self.files.sending:
            self.set_error("Завершите или отмените передачу файла перед выключением.")
            self.refresh()
            return

        def active() -> bool:
            return (
                self.robot is robot
                and self.ssh_session is session
                and self.page_available
                and not self.closed
            )

        password = ft.TextField(label="Пароль sudo, если требуется", password=True)
        self.poweroff_dialog = True
        self.refresh()
        try:
            accepted = await confirm(
                self.page,
                "Выключить Raspberry Pi?",
                ft.Column(
                    [
                        ft.Text(f"{target.username}@{target.host}"),
                        ft.Text("RoboLab остановит запуск и проверит отключение приводов."),
                        password,
                    ],
                    tight=True,
                ),
                "Выключить",
                danger=True,
            )
            if not accepted or not active():
                return
            if self.pending or self.safety_pending is not None:
                self.set_error("Дождитесь завершения текущей команды.")
                return
            self.poweroff_pending = True
            self.drop_lease()
            self.set_error("")
            self.refresh()
            try:
                sent = await safe_poweroff(robot, session, password.value or "", active)
            except PoweroffUnconfirmed:
                await self.disconnect()
                self.connection_status.value = "Связь потеряна. Проверьте, выключился ли Pi."
            except (RobotError, SshError) as exc:
                if active():
                    self.set_error(str(exc))
            else:
                if sent:
                    await self.disconnect()
                    self.connection_status.value = "Отправлена команда выключения Pi"
        finally:
            password.value = ""
            self.poweroff_dialog = self.poweroff_pending = False
            self.refresh()

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
        await self.manual.deactivate()
        if self.ssh_session is not None or self.ssh_task is not None:
            await self.disconnect()
            return
        self.drop_lease()
        task = self.close_observability()
        files_task = self.close_files()
        camera_task = self.close_camera()
        for pending in (task, files_task, camera_task):
            if pending is not None:
                with suppress(asyncio.CancelledError):
                    await pending

    async def close(self, event: ft.ControlEvent) -> None:
        self.closed = True
        await self.disconnect()

    def manual_robot(self) -> RobotConnection | None:
        robot = self.robot
        current = self.current
        if (
            self.closed
            or not self.page_available
            or not self.connected
            or self.disconnecting
            or self.files_open
            or self.observability is not None
            or self.pending
            or self.poweroff_dialog
            or self.poweroff_pending
            or robot is None
            or not getattr(robot, "manual_available", False)
            or (
                robot.kind == "yahboom"
                and (self.safety is None or self.safety.latched or self.safety_pending is robot)
            )
            or (
                current is not None
                and current.script_id != MANUAL_SCRIPT_ID
                and current.status in ("running", "stopping")
            )
        ):
            return None
        return robot

    async def manual_lifecycle(self, event: ft.AppLifecycleStateChangeEvent) -> None:
        if event.state in (
            ft.AppLifecycleState.HIDE,
            ft.AppLifecycleState.PAUSE,
            ft.AppLifecycleState.DETACH,
        ):
            await self.manual.deactivate()

    def refresh_camera(self, hardware_run_active: bool) -> None:
        robot = self.robot
        available = self.connected and robot is not None and robot.camera_available
        if available and self.camera is None:
            self.camera = CameraPanel(self.page, robot, self.camera_picker, self.camera_closed)
            self.camera.set_compact(self.compact)
            self.camera_slot.content = self.camera.content
        self.camera_slot.visible = available and self.camera is not None and not self.camera.closed
        self.journal_slot.col = {"xs": 12, "md": 6} if self.camera_slot.visible else 12
        if self.camera_slot.visible:
            self.camera.set_run_active(hardware_run_active)

    def close_camera(self) -> asyncio.Task[None] | None:
        camera = self.camera
        if camera is None:
            return None
        task = camera.task
        camera.close(update=self.page_available and not self.closed)
        return task

    def camera_closed(self, camera: CameraPanel) -> None:
        if self.camera is camera:
            self.camera = None
            self.camera_slot.content = None
            self.refresh()

    def observability_robot(self) -> RobotConnection | None:
        robot = self.robot
        if (
            not self.closed
            and self.page_available
            and self.connected
            and robot is not None
            and robot.observability_available
        ):
            return robot
        return None

    async def open_telemetry(self, event: ft.ControlEvent) -> None:
        robot = self.observability_robot()
        if robot is not None and not self.telemetry_button.disabled:
            await self.manual.deactivate()
            self.show_observability("Телеметрия", TelemetryPanel(robot))

    async def open_experiments(self, event: ft.ControlEvent) -> None:
        robot = self.observability_robot()
        if robot is not None and not self.experiments_button.disabled:
            await self.manual.deactivate()
            self.show_observability(
                "Эксперименты", ExperimentsPanel(self.page, robot, self.camera_picker)
            )

    def show_observability(self, title: str, panel: TelemetryPanel | ExperimentsPanel) -> None:
        self.close_observability()
        self.files_open = False
        self.observability = panel
        self.observability_content.controls = [
            ft.Row(
                [
                    ft.Text(title, size=17, weight=ft.FontWeight.W_600),
                    ft.TextButton("Закрыть", on_click=self.dismiss_observability),
                ],
                alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                wrap=True,
            ),
            panel.content,
        ]
        self.observability_card.visible = True
        self.refresh()
        panel.start()

    def close_observability(self) -> asyncio.Task[None] | None:
        panel, self.observability = self.observability, None
        if panel is None:
            return None
        self.observability_card.visible = False
        self.observability_content.controls.clear()
        return panel.close()

    async def dismiss_observability(self, event: ft.ControlEvent) -> None:
        task = self.close_observability()
        self.refresh()
        if task is not None:
            with suppress(asyncio.CancelledError):
                await task

    async def show_run(self, event: ft.ControlEvent) -> None:
        self.files_open = False
        task = self.close_observability()
        self.refresh()
        if task is not None:
            with suppress(asyncio.CancelledError):
                await task

    async def open_files(self, event: ft.ControlEvent) -> None:
        if self.closed or not self.page_available:
            return
        session, robot = self.ssh_session, self.robot
        if session is not None and not session.closed:
            files = session.files
        elif self.connected and robot is not None and robot.files_available:
            files = robot
        else:
            return
        await self.manual.deactivate()
        self.close_observability()
        self.files_open = True
        if self.files is None:
            self.files = FilePanel(
                self.page, files, self.camera_picker, self.show_run, self.compact
            )
            self.files_view.content = self.files.content
            self.refresh()
            self.files.start()
        else:
            self.refresh()

    def close_files(self) -> asyncio.Task[None] | None:
        panel, self.files = self.files, None
        self.files_open = False
        self.files_view.content = None
        return panel.close() if panel is not None else None


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
