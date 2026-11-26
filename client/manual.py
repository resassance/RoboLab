import asyncio
from collections.abc import Callable
from typing import Literal

import flet as ft

from client.appearance import MUTED, button_row
from client.connection import RobotConnection, RobotError
from common.protocol import MANUAL_HEARTBEAT_TIMEOUT, ManualDirection, RunStarted, RunState

ManualCommand = ManualDirection | Literal["stop"]
COMMANDS: dict[str, ManualCommand] = {
    "вперед": "forward",
    "назад": "backward",
    "влево": "left",
    "вправо": "right",
    "стоп": "stop",
}
TITLES: dict[ManualCommand, str] = {
    "forward": "Вперёд",
    "backward": "Назад",
    "left": "Влево на 45°",
    "right": "Вправо на 45°",
    "stop": "Стоп",
}
HEARTBEAT_PERIOD = 0.25


def spoken_command(phrase: str) -> ManualCommand | None:
    return COMMANDS.get(phrase.strip().casefold().replace("ё", "е"))


class ManualPanel:
    def __init__(
        self,
        page: ft.Page,
        ready_robot: Callable[[], RobotConnection | None],
        refresh: Callable[[], None],
    ) -> None:
        self.ready_robot = ready_robot
        self.refresh = refresh
        self.voice = None
        if not page.web:
            from client.voice import VoiceInput

            self.voice = VoiceInput(page, self.on_phrase, self.on_voice_error)
            if self.voice.control is not None:
                page.services.append(self.voice.control)
        self.voice_lock = asyncio.Lock()
        self.voice_requested = False
        self.last_phrase: tuple[ManualCommand, float] | None = None
        self.lock = asyncio.Lock()
        self.generation = 0
        self.owned: tuple[RobotConnection, RunStarted, ManualDirection] | None = None
        self.heartbeat_task: asyncio.Task[None] | None = None
        self.status = ft.Text(color=MUTED, size=13)
        self.voice_status = ft.Text(color=MUTED, size=13, visible=self.voice is not None)
        self.microphone = ft.OutlinedButton(
            "Включить голос",
            icon=ft.Icons.MIC_NONE,
            on_click=self.toggle_voice,
            visible=self.voice is not None,
        )
        self.buttons = {
            command: ft.OutlinedButton(title, data=command, on_click=self.press, disabled=True)
            for command, title in TITLES.items()
        }
        self.content = ft.Column(
            [
                ft.Text("Ручное управление", size=16, weight=ft.FontWeight.W_600),
                button_row(*(self.buttons[item] for item in ("forward", "backward", "stop"))),
                button_row(self.buttons["left"], self.buttons["right"], self.microphone),
                self.voice_status,
                self.status,
            ],
            visible=False,
            spacing=6,
        )

    def update(self, available: bool) -> None:
        self.content.visible = available
        disabled = self.ready_robot() is None
        for command, button in self.buttons.items():
            button.disabled = (
                disabled
                if command != "stop"
                else disabled and not (self.owned or self.lock.locked())
            )
        self.microphone.disabled = disabled and not self.voice_requested
        self.microphone.content = "Выключить голос" if self.voice_requested else "Включить голос"

    async def press(self, event: ft.ControlEvent) -> None:
        await self.command(event.control.data)

    async def toggle_voice(self, event: ft.ControlEvent) -> None:
        if self.voice is None:
            return
        if self.voice_requested:
            await self.deactivate()
            return
        if self.ready_robot() is None:
            return
        self.voice_requested = True
        self.voice_status.value = "Подключается микрофон…"
        self.refresh()
        async with self.voice_lock:
            if not self.voice_requested:
                return
            try:
                await self.voice.start()
            except RuntimeError as exc:
                self.voice_requested = False
                self.voice_status.value = str(exc)
            else:
                if self.voice_requested:
                    self.voice_status.value = "Слушаю. Скажите одну из пяти команд."
            finally:
                self.refresh()

    async def deactivate(self) -> None:
        self.voice_requested = False
        self.last_phrase = None
        self.voice_status.value = ""
        await asyncio.gather(self.command("stop", quiet=True), self.close_microphone())

    async def close_microphone(self) -> None:
        if self.voice is not None:
            async with self.voice_lock:
                await self.voice.stop()

    async def on_phrase(self, phrase: str) -> None:
        if not self.voice_requested:
            return
        command = spoken_command(phrase)
        if command != "stop" and self.ready_robot() is None:
            return
        if command is None:
            self.voice_status.value = "Команда не распознана."
            self.refresh()
            return
        now = asyncio.get_running_loop().time()
        previous = self.last_phrase
        if command != "stop" and previous and previous[0] == command and now - previous[1] < 1:
            return
        self.last_phrase = (command, now)
        self.voice_status.value = f"Распознано «{TITLES[command].lower()}»."
        self.refresh()
        await asyncio.shield(self.voice_command(command))

    async def voice_command(self, command: ManualCommand) -> None:
        if self.voice_requested:
            await self.command(command)

    async def on_voice_error(self, message: str) -> None:
        self.voice_requested = False
        self.last_phrase = None
        self.voice_status.value = message
        await self.command("stop", quiet=True)

    async def command(self, command: ManualCommand, *, quiet: bool = False) -> None:
        robot = self.ready_robot()
        if command != "stop" and robot is None:
            return
        if self.owned is not None and self.owned[2] == command and not self.lock.locked():
            return
        self.generation += 1
        generation = self.generation
        if command == "stop":
            self.cancel_heartbeat()
        async with self.lock:
            if generation != self.generation:
                return
            try:
                had_run = self.owned is not None
                if self.owned is not None:
                    await self.stop_owned()
                if command == "stop":
                    if had_run or not quiet:
                        self.status.value = "Ручное движение остановлено."
                    return
                if generation != self.generation or robot is not self.ready_robot():
                    return
                assert robot is not None
                self.status.value = f"Отправляется команда «{TITLES[command].lower()}»."
                self.refresh()
                started = await robot.start_manual(command)
                self.owned = (robot, started, command)
                if generation != self.generation or robot is not self.ready_robot():
                    await self.stop_owned()
                    return
                self.heartbeat_task = asyncio.create_task(self.keep_lease(robot, started))
                self.status.value = f"Робот принял команду «{TITLES[command].lower()}»."
            except RobotError as exc:
                self.status.value = str(exc)
            finally:
                self.refresh()

    async def stop_owned(self) -> None:
        self.cancel_heartbeat()
        assert self.owned is not None
        owned = self.owned
        robot, started, _ = owned
        assert started.lease is not None
        await robot.stop_manual(started.id, started.lease)
        if self.owned is owned:
            self.owned = None

    def cancel_heartbeat(self) -> None:
        if self.heartbeat_task is not None:
            self.heartbeat_task.cancel()
            self.heartbeat_task = None

    def observe(self, run: RunState | None) -> None:
        if self.owned is None or run is None or run.id != self.owned[1].id:
            return
        if run.status not in ("running", "stopping"):
            self.cancel_heartbeat()
            self.owned = None
            self.status.value = (
                "Агент сообщил об ошибке движения."
                if run.status == "failed"
                else "Ручное движение завершено."
            )

    async def keep_lease(self, robot: RobotConnection, started: RunStarted) -> None:
        assert started.lease is not None
        loop = asyncio.get_running_loop()
        deadline = loop.time() + MANUAL_HEARTBEAT_TIMEOUT
        while (sent := loop.time()) < deadline:
            try:
                async with asyncio.timeout(HEARTBEAT_PERIOD):
                    await robot.heartbeat(started.id, started.lease)
            except TimeoutError:
                pass
            except RobotError as exc:
                if not exc.retryable:
                    break
            else:
                deadline = sent + MANUAL_HEARTBEAT_TIMEOUT
            await asyncio.sleep(max(0, sent + HEARTBEAT_PERIOD - loop.time()))
