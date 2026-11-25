from datetime import UTC, datetime
from typing import Literal

import flet as ft

from client.appearance import (
    AMBER,
    CONTROL_RADIUS,
    ERROR,
    LINE,
    MUTED,
    PRIMARY,
    PRIMARY_TINT,
    SUCCESS,
    SURFACE,
    TEXT,
    button_row,
)
from common.protocol import LOG_LIMIT, LogLine, RunState, SafetyState

JournalMode = Literal["summary", "full"]
TERMINAL = "#172238"
TERMINAL_TEXT = "#ECF1FE"
TERMINAL_WARNING = "#FFD090"
TERMINAL_ERROR = "#FFABA6"
RUN_NOTES = {
    "running": ("Запуск начат.", "info"),
    "stopping": ("Запрошена остановка.", "warning"),
    "succeeded": ("Запуск завершён.", "success"),
    "stopped": ("Запуск остановлен.", "warning"),
    "failed": ("Ошибка запуска.", "error"),
}
NOTE_STYLES = {
    "info": (ft.Icons.INFO_OUTLINE, PRIMARY),
    "success": (ft.Icons.CHECK_CIRCLE_OUTLINE, SUCCESS),
    "warning": (ft.Icons.WARNING_AMBER_OUTLINED, AMBER),
    "error": (ft.Icons.ERROR_OUTLINE, ERROR),
}


class RunJournal:
    def __init__(self) -> None:
        self.summary = ft.ListView(expand=True, auto_scroll=True, spacing=8)
        self.full = ft.ListView(expand=True, auto_scroll=True, spacing=4)
        self.summary_empty = ft.Text("Важные события появятся после запуска.", color=MUTED)
        self.full_empty = ft.Text("Ожидание вывода…", color=TERMINAL_TEXT, font_family="monospace")
        self.gap = ft.Text("Часть предыдущих сообщений пропущена.", color=AMBER, visible=False)
        self.summary_button = ft.OutlinedButton(
            "Важные события", height=42, on_click=self.show_summary
        )
        self.full_button = ft.OutlinedButton("Полный журнал", height=42, on_click=self.show_full)
        self.timeline = ft.Container(
            content=ft.Column([self.summary_empty, self.summary], spacing=6),
            height=228,
            padding=12,
            border=ft.Border.all(1, LINE),
            border_radius=CONTROL_RADIUS,
            bgcolor=SURFACE,
        )
        self.terminal = ft.Container(
            content=ft.Column(
                [self.full_empty, ft.SelectionArea(content=self.full, expand=True)],
                spacing=6,
            ),
            height=228,
            padding=12,
            bgcolor=TERMINAL,
            border_radius=CONTROL_RADIUS,
        )
        self.content = ft.Column(
            [
                ft.Text("Журнал", size=21, weight=ft.FontWeight.W_700, color=TEXT),
                button_row(self.summary_button, self.full_button),
                self.gap,
                self.timeline,
                self.terminal,
            ],
            spacing=9,
            horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
        )
        self.set_mode("summary")

    def set_compact(self, compact: bool) -> None:
        self.timeline.height = self.terminal.height = 156 if compact else 295

    def set_mode(self, mode: JournalMode) -> None:
        self.timeline.visible = mode == "summary"
        self.terminal.visible = mode == "full"
        for active, button in (
            (self.timeline.visible, self.summary_button),
            (self.terminal.visible, self.full_button),
        ):
            button.style = ft.ButtonStyle(
                shape=ft.RoundedRectangleBorder(radius=CONTROL_RADIUS),
                bgcolor=PRIMARY_TINT if active else SURFACE,
                color=PRIMARY if active else TEXT,
                side=ft.BorderSide(1, PRIMARY if active else LINE),
                padding=ft.Padding.symmetric(horizontal=6),
            )

    async def show_summary(self, event: ft.ControlEvent) -> None:
        self.set_mode("summary")
        self.content.update()

    async def show_full(self, event: ft.ControlEvent) -> None:
        self.set_mode("full")
        self.content.update()

    def reset(self) -> None:
        self.summary.controls.clear()
        self.full.controls.clear()
        self.summary_empty.visible = self.full_empty.visible = True
        self.gap.visible = False

    def note(self, message: str, kind: str, when: datetime) -> None:
        icon, color = NOTE_STYLES[kind]
        self.summary.controls.append(
            ft.Row(
                [
                    ft.Icon(icon, color=color, size=16),
                    ft.Text(when.astimezone().strftime("%H:%M:%S"), size=11, color=MUTED),
                    ft.Text(message, size=13, color=TEXT, expand=True),
                ],
                spacing=7,
                vertical_alignment=ft.CrossAxisAlignment.START,
            )
        )
        del self.summary.controls[:-LOG_LIMIT]
        self.summary_empty.visible = False

    def record_run(self, previous: RunState | None, current: RunState | None) -> None:
        if current is None or (
            previous is not None and previous.id == current.id and previous.status == current.status
        ):
            return
        message, kind = RUN_NOTES[current.status]
        if current.status == "failed" and current.exit_code is not None:
            message = f"{message} Код завершения {current.exit_code}."
        when = current.started_at if current.status == "running" else datetime.now(UTC)
        self.note(message, kind, when)

    def record_safety(self, previous: SafetyState | None, state: SafetyState | None) -> None:
        if state is None:
            return
        if state.latched and (
            previous is None or not previous.latched or previous.reason != state.reason
        ):
            detail = f" {state.reason}" if state.reason else ""
            self.note(f"Аварийная блокировка включена.{detail}", "error", datetime.now(UTC))
        if (
            state.latched
            and state.motor_disable_confirmed
            and (previous is None or previous.motor_disable_confirmed is not True)
        ):
            self.note("Отключение приводов подтверждено.", "success", datetime.now(UTC))
        if not state.latched and previous is not None and previous.latched:
            self.note("Аварийная блокировка снята.", "info", datetime.now(UTC))

    def record_line(self, line: LogLine) -> None:
        stamp = line.timestamp.astimezone().strftime("%H:%M:%S")
        color = {"ERROR": TERMINAL_ERROR, "WARNING": TERMINAL_WARNING}.get(
            line.level, TERMINAL_TEXT
        )
        self.full.controls.append(
            ft.Text(
                f"{stamp}  {line.level}  {line.source}  {line.message}",
                size=12,
                color=color,
                font_family="monospace",
            )
        )
        del self.full.controls[:-LOG_LIMIT]
        self.full_empty.visible = False
        if line.level in ("WARNING", "ERROR") or line.source == "agent":
            kind = {"WARNING": "warning", "ERROR": "error"}.get(line.level, "info")
            self.note(line.message, kind, line.timestamp)
