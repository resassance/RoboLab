import asyncio
import shlex

import flet as ft
from pydantic import TypeAdapter

from client.appearance import (
    CONTROL_RADIUS,
    ERROR,
    ERROR_TINT,
    MUTED,
    SUCCESS,
    SUCCESS_TINT,
    TEAL,
    TEAL_TINT,
    TEXT,
    button_row,
)
from common.parameters import Parameter, Value
from common.protocol import Script

PREFERENCES_TIMEOUT = 4
VALUES = TypeAdapter(dict[str, Value])


class ConfigurationStore:
    def __init__(self) -> None:
        self.preferences = ft.SharedPreferences()

    async def load(self, key: str) -> dict[str, Value]:
        raw = await asyncio.wait_for(self.preferences.get(key), PREFERENCES_TIMEOUT)
        return VALUES.validate_json(raw) if isinstance(raw, str) else {}

    async def current(self, key: str, script: Script) -> dict[str, Value]:
        try:
            return script.configuration(await self.load(f"{key}.last"))
        except ValueError:
            return script.configuration({})

    async def preset_names(self, key: str) -> set[str]:
        prefix = f"{key}.preset."
        keys = await asyncio.wait_for(self.preferences.get_keys(prefix), PREFERENCES_TIMEOUT)
        return {name.removeprefix(prefix) for name in keys}

    async def save(self, key: str, values: dict[str, Value]) -> None:
        raw = VALUES.dump_json(values).decode("utf-8")
        if not await asyncio.wait_for(self.preferences.set(key, raw), PREFERENCES_TIMEOUT):
            raise RuntimeError("Не удалось сохранить настройки на этом устройстве.")


class ParameterDialog:
    def __init__(
        self,
        page: ft.Page,
        script: Script,
        key: str,
        store: ConfigurationStore,
        initial: dict[str, Value],
        preset_names: set[str],
    ) -> None:
        self.page = page
        self.script = script
        self.key = key
        self.store = store
        self.initial = initial
        self.preset_names = preset_names
        self.inputs: dict[str, ft.Switch | ft.RadioGroup | ft.TextField] = {}
        self.fields: dict[str, ft.Column] = {}
        self.preview = ft.TextField(
            label="Параметры для агента" if script.requires_heartbeat else "Команда (POSIX)",
            read_only=True,
            multiline=True,
            max_lines=4,
            text_style=ft.TextStyle(font_family="monospace"),
        )
        self.error = ft.Text(color=ERROR, visible=False)
        self.error_notice = ft.Container(
            self.error, padding=10, bgcolor=ERROR_TINT, border_radius=CONTROL_RADIUS, visible=False
        )
        self.preset_name = ft.TextField(label="Название пресета", max_length=60)
        self.preset_buttons = ft.Column(
            spacing=6, horizontal_alignment=ft.CrossAxisAlignment.STRETCH
        )
        self.presets = ft.Column(
            [
                ft.Text("Пресеты", weight=ft.FontWeight.W_600, color=TEAL),
                self.preset_buttons,
            ],
            visible=False,
        )
        self.saved = ft.Text("Пресет сохранён.", color=SUCCESS, visible=False)
        self.saved_notice = ft.Container(
            self.saved,
            padding=10,
            bgcolor=SUCCESS_TINT,
            border_radius=CONTROL_RADIUS,
            visible=False,
        )
        self.save_button = ft.OutlinedButton(
            "Сохранить пресет", height=44, on_click=self.save_preset
        )
        self.apply_button = ft.FilledButton("Применить", height=44, on_click=self.apply)
        self.saving = False
        self.dialog: ft.AlertDialog
        self.set_preset_options()

    def show(self) -> None:
        controls: list[ft.Control] = [self.presets]
        groups: dict[str, ft.Column] = {}

        for parameter in self.script.parameters:
            if parameter.group not in groups:
                group = ft.Column(
                    [ft.Text(parameter.group, weight=ft.FontWeight.W_600, color=TEXT)], spacing=8
                )
                groups[parameter.group] = group
                controls.append(group)
            control = self.create_input(parameter)
            field = ft.Column(
                [control, ft.Text(parameter.description, size=12, color=MUTED)],
                spacing=3,
            )
            self.inputs[parameter.key] = control
            self.fields[parameter.key] = field
            groups[parameter.group].controls.append(field)

        controls.extend(
            [
                ft.Container(
                    self.preview, padding=10, bgcolor=TEAL_TINT, border_radius=CONTROL_RADIUS
                ),
                self.error_notice,
                self.preset_name,
                self.save_button,
                self.saved_notice,
            ]
        )
        self.dialog = ft.AlertDialog(
            modal=True,
            title=ft.Text(f"Параметры · {self.script.name}"),
            content=ft.Container(
                width=min(460, max(210, (self.page.width or 520) - 88)),
                content=ft.Column(
                    controls, spacing=12, horizontal_alignment=ft.CrossAxisAlignment.STRETCH
                ),
            ),
            scrollable=True,
            inset_padding=ft.Padding.symmetric(horizontal=20, vertical=18),
            actions=[
                button_row(
                    ft.OutlinedButton("Отмена", height=44, on_click=self.cancel),
                    self.apply_button,
                )
            ],
        )
        self.fill(self.initial)
        self.update_preview()
        self.page.show_dialog(self.dialog)

    def create_input(self, parameter: Parameter) -> ft.Switch | ft.RadioGroup | ft.TextField:
        if parameter.kind == "bool":
            return ft.Switch(label=parameter.title, on_change=self.changed)
        if parameter.kind == "enum":
            return ft.RadioGroup(
                content=ft.Column(
                    [
                        ft.Text(parameter.title),
                        ft.Row(
                            [ft.Radio(value=choice, label=choice) for choice in parameter.choices],
                            wrap=True,
                        ),
                    ],
                    spacing=2,
                ),
                on_change=self.changed,
            )
        return ft.TextField(label=parameter.title, on_change=self.changed)

    def fill(self, values: dict[str, Value]) -> None:
        for parameter in self.script.parameters:
            control = self.inputs[parameter.key]
            value = values.get(parameter.key, parameter.default)
            if isinstance(control, ft.Switch):
                control.value = bool(value)
            else:
                control.value = str(value)

    def values(self) -> dict[str, Value]:
        values: dict[str, Value] = {}
        first_error = None
        for parameter in self.script.parameters:
            field = self.fields[parameter.key]
            field.visible = parameter.enabled(values)
            if not field.visible:
                continue
            control = self.inputs[parameter.key]
            value = control.value
            try:
                if isinstance(control, ft.Switch):
                    value = bool(value)
                elif parameter.kind == "int":
                    value = int(value or "")
                elif parameter.kind == "float":
                    value = float((value or "").replace(",", "."))
            except ValueError:
                first_error = first_error or f"«{parameter.title}»: введите число."
                continue
            try:
                values[parameter.key] = parameter.validate_value(value)
            except ValueError as exc:
                first_error = first_error or str(exc)
        if first_error:
            raise ValueError(first_error)
        return values

    def update_preview(self) -> None:
        try:
            arguments = self.script.arguments(self.values())
            if not self.script.requires_heartbeat:
                arguments = ["python", self.script.entrypoint or self.script.id, *arguments]
            self.preview.value = shlex.join(arguments)
            self.set_error("")
            self.apply_button.disabled = self.saving
            self.save_button.disabled = self.saving
        except ValueError as exc:
            self.preview.value = ""
            self.set_error(str(exc))
            self.apply_button.disabled = True
            self.save_button.disabled = True

    def set_error(self, message: str) -> None:
        self.error.value = message
        self.error.visible = self.error_notice.visible = bool(message)

    def set_preset_options(self) -> None:
        self.preset_buttons.controls = [
            ft.OutlinedButton(
                ft.Text(name, text_align=ft.TextAlign.CENTER),
                data=name,
                min_height=44,
                on_click=self.use_preset,
            )
            for name in sorted(self.preset_names)
        ]
        self.presets.visible = bool(self.preset_buttons.controls)

    async def changed(self, event: ft.ControlEvent) -> None:
        self.saved.visible = self.saved_notice.visible = False
        self.update_preview()
        self.page.update()

    async def use_preset(self, event: ft.ControlEvent) -> None:
        if self.saving:
            return
        name = event.control.data
        try:
            selected = await self.store.load(f"{self.key}.preset.{name}")
            values = self.script.configuration(selected)
        except ValueError:
            self.set_error("Этот пресет несовместим с текущим манифестом.")
        except (RuntimeError, TimeoutError):
            self.set_error("Не удалось прочитать пресет.")
        else:
            self.fill(values)
            self.update_preview()
        self.page.update()

    async def save(self, key: str, values: dict[str, Value]) -> bool:
        self.saving = True
        self.update_preview()
        self.page.update()
        success = False
        try:
            await self.store.save(key, values)
            success = True
        except (RuntimeError, TimeoutError):
            pass
        finally:
            self.saving = False
            self.update_preview()
            if not success:
                self.set_error("Не удалось сохранить настройки на этом устройстве.")
            self.page.update()
        return success

    async def save_preset(self, event: ft.ControlEvent) -> None:
        if self.save_button.disabled:
            return
        self.saved.visible = self.saved_notice.visible = False
        name = (self.preset_name.value or "").strip()
        if not name or len(name) > 60 or not name.isprintable():
            self.set_error("Введите название пресета, до 60 печатных символов.")
            self.page.update()
            return
        if await self.save(f"{self.key}.preset.{name}", self.values()):
            self.preset_names.add(name)
            self.set_preset_options()
            self.saved.visible = self.saved_notice.visible = True
            self.page.update()

    async def apply(self, event: ft.ControlEvent) -> None:
        if self.apply_button.disabled:
            return
        if await self.save(f"{self.key}.last", self.values()):
            self.page.pop_dialog()

    async def cancel(self, event: ft.ControlEvent) -> None:
        if not self.saving:
            self.page.pop_dialog()
