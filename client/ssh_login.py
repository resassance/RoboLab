import asyncio
from collections.abc import Callable
from hashlib import sha256

import flet as ft
from flet_secure_storage import SecureStorage
from pydantic import BaseModel

from client.appearance import (
    CONTROL_RADIUS,
    DANGER,
    ERROR,
    ERROR_TINT,
    MUTED,
    PRIMARY_TINT,
    SUCCESS,
    SUCCESS_TINT,
    TEXT,
    button_row,
)
from client.connection import RobotConnection, RobotError
from client.ssh import HostKeyChanged, SshError, SshSession, SshTarget, UnknownHostKey

PROFILE_KEY = "robolab.ssh.profile"
STORAGE_TIMEOUT = 4


class SavedProfile(BaseModel):
    host: str
    port: int
    username: str
    password_saved: bool

    @property
    def target(self) -> SshTarget:
        return SshTarget(self.host, self.port, self.username)


class SshProfiles:
    def __init__(self) -> None:
        self.preferences = ft.SharedPreferences()
        self.secrets = SecureStorage()

    async def profile(self) -> SavedProfile | None:
        raw = await self.preferences.get(PROFILE_KEY)
        if isinstance(raw, str):
            try:
                profile = SavedProfile.model_validate_json(raw)
                profile.target
                return profile
            except ValueError:
                return None
        return None

    @staticmethod
    def password_key(target: SshTarget) -> str:
        return (
            "robolab.ssh.password."
            + sha256(f"{target.host}\0{target.port}\0{target.username}".encode()).hexdigest()
        )

    @staticmethod
    def host_key(target: SshTarget) -> str:
        return "robolab.ssh.host." + sha256(f"{target.host}\0{target.port}".encode()).hexdigest()

    async def fingerprint(self, target: SshTarget) -> str | None:
        async with asyncio.timeout(STORAGE_TIMEOUT):
            value = await self.preferences.get(self.host_key(target))
        if value is not None and not isinstance(value, str):
            raise RuntimeError("Сохранённый ключ SSH не удалось прочитать.")
        return value

    async def trust(self, target: SshTarget, fingerprint: str) -> None:
        async with asyncio.timeout(STORAGE_TIMEOUT):
            if not await self.preferences.set(self.host_key(target), fingerprint):
                raise RuntimeError("Не удалось сохранить ключ SSH.")

    async def load(self) -> tuple[SshTarget, str | None] | None:
        async with asyncio.timeout(STORAGE_TIMEOUT):
            profile = await self.profile()
        if profile is None:
            return None
        password = None
        if profile.password_saved:
            try:
                async with asyncio.timeout(STORAGE_TIMEOUT):
                    password = await self.secrets.get(self.password_key(profile.target))
            except (RuntimeError, TimeoutError):
                pass
        return profile.target, password

    async def save(self, target: SshTarget, password: str | None) -> None:
        async with asyncio.timeout(STORAGE_TIMEOUT):
            previous = await self.profile()
            if previous and previous.password_saved and (previous.target != target or not password):
                await self.secrets.remove(self.password_key(previous.target))
            profile = SavedProfile(
                host=target.host,
                port=target.port,
                username=target.username,
                password_saved=bool(password),
            )
            if not await self.preferences.set(PROFILE_KEY, profile.model_dump_json()):
                raise RuntimeError("Не удалось сохранить SSH-профиль.")
            if password:
                await self.secrets.set(self.password_key(target), password)

    async def forget_key(self, target: SshTarget) -> None:
        async with asyncio.timeout(STORAGE_TIMEOUT):
            if not await self.preferences.remove(self.host_key(target)):
                raise RuntimeError("Не удалось удалить ключ SSH.")

    async def forget(self) -> None:
        async with asyncio.timeout(STORAGE_TIMEOUT):
            profile = await self.profile()
            if profile is not None:
                if profile.password_saved:
                    await self.secrets.remove(self.password_key(profile.target))
                await self.preferences.remove(self.host_key(profile.target))
                if not await self.preferences.remove(PROFILE_KEY):
                    raise RuntimeError("Не удалось удалить SSH-профиль.")


async def confirm(
    page: ft.Page, title: str, content: ft.Control, action: str, *, danger: bool = False
) -> bool:
    result: asyncio.Future[bool] = asyncio.get_running_loop().create_future()

    def answer(value: bool) -> None:
        if not result.done():
            result.set_result(value)

    def accept(event: ft.ControlEvent) -> None:
        answer(True)

    def decline(event: ft.ControlEvent) -> None:
        answer(False)

    dialog = ft.AlertDialog(
        modal=True,
        title=ft.Text(title),
        content=content,
        scrollable=True,
        inset_padding=ft.Padding.symmetric(horizontal=20, vertical=18),
        actions=[
            button_row(
                ft.OutlinedButton("Отмена", height=44, on_click=decline),
                ft.FilledButton(
                    ft.Text(action, text_align=ft.TextAlign.CENTER),
                    height=44,
                    on_click=accept,
                    style=ft.ButtonStyle(bgcolor=DANGER) if danger else None,
                ),
            )
        ],
        on_dismiss=decline,
    )
    page.show_dialog(dialog)
    try:
        return await result
    finally:
        if dialog.open:
            page.pop_dialog()


class SshLogin:
    def __init__(self, page: ft.Page) -> None:
        self.page = page
        self.profiles = SshProfiles()
        self.loading = True
        self.forgetting = False
        self.saved: SshTarget | None = None
        self.mismatched: SshTarget | None = None
        self.saved_password = False
        self.host = ft.TextField(label="Адрес Raspberry Pi", on_change=self.target_changed)
        self.port = ft.TextField(
            label="Порт SSH",
            value="22",
            keyboard_type=ft.KeyboardType.NUMBER,
            on_change=self.target_changed,
        )
        self.username = ft.TextField(label="Имя пользователя SSH", on_change=self.target_changed)
        self.password = ft.TextField(label="Пароль SSH", password=True, can_reveal_password=True)
        self.remember = ft.Checkbox(label="Запомнить пароль", value=False)
        self.error = ft.Text(color=ERROR, visible=False)
        self.feedback = ft.Container(
            self.error,
            bgcolor=ERROR_TINT,
            border_radius=CONTROL_RADIUS,
            padding=12,
            visible=False,
        )
        self.forget_button = ft.OutlinedButton(
            "Забыть подключение", height=44, on_click=self.forget, visible=False
        )
        self.forget_key_button = ft.OutlinedButton(
            "Забыть ключ SSH", height=44, on_click=self.forget_mismatched_key, visible=False
        )
        self.control = ft.Column(
            [
                self.host,
                ft.ResponsiveRow(
                    [
                        ft.Container(self.username, col={"xs": 8}),
                        ft.Container(self.port, col={"xs": 4}),
                    ],
                    spacing=8,
                ),
                self.password,
                self.remember,
                self.feedback,
                self.forget_button,
                self.forget_key_button,
            ],
            horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
        )

    def message(self, message: str, *, success: bool = False) -> None:
        self.error.value = message
        self.error.color = SUCCESS if success else ERROR
        self.feedback.bgcolor = SUCCESS_TINT if success else ERROR_TINT
        self.error.visible = self.feedback.visible = bool(message)

    async def restore(self) -> None:
        try:
            loaded = await self.profiles.load()
            if loaded is not None:
                target, password = loaded
                self.host.value, self.port.value, self.username.value = (
                    target.host,
                    str(target.port),
                    target.username,
                )
                self.saved = target
                self.saved_password = bool(password)
                self.remember.value = self.saved_password
                self.target_changed(None)
        except (RuntimeError, TimeoutError):
            self.message("Сохранённое подключение недоступно. Введите данные вручную.")
        finally:
            self.loading = False

    def target(self) -> SshTarget:
        try:
            port = int(self.port.value or "")
        except ValueError:
            raise ValueError("Введите порт SSH от 1 до 65535.") from None
        return SshTarget(self.host.value or "", port, self.username.value or "")

    def target_changed(self, event: ft.ControlEvent | None) -> None:
        try:
            target = self.target()
        except ValueError:
            target = None
        same_target = target is not None and target == self.saved
        self.forget_key_button.visible = target is not None and target == self.mismatched
        self.password.hint_text = "Пароль сохранён" if same_target and self.saved_password else None
        if not same_target:
            self.remember.value = False

    async def connect(self) -> tuple[SshTarget, SshSession]:
        target = self.target()
        password = self.password.value or ""
        if not password and self.remember.value and target == self.saved:
            profile = await self.profiles.load()
            if profile is not None and profile[0] == target:
                password = profile[1] or ""
        if not password:
            raise ValueError("Введите пароль SSH.")
        fingerprint = await self.profiles.fingerprint(target)
        try:
            session = await SshSession.connect(target, password, fingerprint)
        except HostKeyChanged:
            self.mismatched = target
            self.forget_key_button.visible = True
            raise
        except UnknownHostKey as exc:
            verified = await confirm(
                self.page,
                "Проверка ключа Raspberry Pi",
                ft.Column(
                    [
                        ft.Text(f"{target.host}:{target.port}", color=MUTED),
                        ft.Container(
                            ft.Text(
                                exc.fingerprint,
                                font_family="monospace",
                                color=TEXT,
                                selectable=True,
                            ),
                            bgcolor=PRIMARY_TINT,
                            border_radius=CONTROL_RADIUS,
                            padding=12,
                        ),
                        ft.Text("Сверьте отпечаток с ключом SSH на Raspberry Pi.", color=MUTED),
                    ],
                    tight=True,
                ),
                "Доверять ключу",
            )
            if not verified:
                raise SshError("Подключение отменено.")
            fingerprint = exc.fingerprint
            session = await SshSession.connect(target, password, fingerprint)
            try:
                await self.profiles.trust(target, fingerprint)
            except BaseException:
                await session.close()
                raise
        try:
            try:
                await self.profiles.save(target, password if self.remember.value else None)
                self.saved, self.saved_password = target, bool(self.remember.value)
            except (RuntimeError, TimeoutError):
                self.message("Подключено. Сохранить данные не удалось.")
            self.password.value = ""
            self.target_changed(None)
        except BaseException:
            await session.close()
            raise
        return target, session

    async def forget(self, event: ft.ControlEvent) -> None:
        if self.forgetting or self.control.disabled:
            return
        self.forgetting = True
        self.forget_button.disabled = True
        self.control.update()
        try:
            await self.profiles.forget()
            self.saved = None
            self.saved_password = False
            self.host.value = self.username.value = self.password.value = ""
            self.port.value = "22"
            self.target_changed(None)
            self.forget_button.visible = False
            self.message("")
        except (RuntimeError, TimeoutError):
            self.message("Не удалось удалить сохранённое подключение.")
        finally:
            self.forgetting = False
            self.forget_button.disabled = False
            self.control.update()

    async def forget_mismatched_key(self, event: ft.ControlEvent) -> None:
        target = self.mismatched
        if target is None or not self.forget_key_button.visible or self.control.disabled:
            return
        self.forget_key_button.disabled = True
        try:
            if await confirm(
                self.page,
                "Забыть ключ SSH?",
                ft.Text(f"При следующем входе сверьте новый ключ устройства {target.host}."),
                "Забыть ключ",
            ):
                await self.profiles.forget_key(target)
                self.mismatched = None
                self.forget_key_button.visible = False
                self.message("Ключ удалён. Подключитесь и проверьте новый отпечаток.", success=True)
        except (RuntimeError, TimeoutError):
            self.message("Не удалось удалить ключ SSH.")
        finally:
            self.forget_key_button.disabled = False
            self.control.update()

    def refresh(self, disabled: bool) -> None:
        self.control.disabled = disabled or self.loading
        self.forget_button.visible = self.saved is not None


async def safe_poweroff(
    robot: RobotConnection,
    session: SshSession,
    password: str,
    active: Callable[[], bool] = lambda: True,
) -> bool:
    state = await robot.prepare_poweroff()
    if not state.latched or state.motor_disable_confirmed is not True:
        raise RobotError("Агент не подтвердил отключение приводов. Выключение отменено.")
    if not active():
        return False
    await session.poweroff(password)
    return True
