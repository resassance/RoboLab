import asyncio
from collections import deque
from collections.abc import Awaitable, Callable
from datetime import datetime
from math import isfinite
from typing import Literal
from uuid import UUID

import flet as ft
import flet.canvas as cv

from client.appearance import (
    AMBER,
    AMBER_TINT,
    CONTROL_RADIUS,
    ERROR,
    ERROR_TINT,
    LINE,
    MUTED,
    PRIMARY,
    PRIMARY_TINT,
    SUBTLE,
    SUCCESS,
    SUCCESS_TINT,
    SURFACE,
    TEAL,
    TEAL_TINT,
    TEXT,
    button_row,
)
from client.connection import RobotConnection, RobotError
from common.protocol import Experiment, ExperimentDetails, MetricSample, SystemTelemetry

RUN_STATUS = {
    "running": "Выполняется",
    "stopping": "Останавливается…",
    "succeeded": "Завершён",
    "stopped": "Остановлен",
    "failed": "Завершён с ошибкой",
    "interrupted": "Прерван",
}
EMPTY = "—"
MEBIBYTE = 2**20
HISTORY_PAGE = 20


def timestamp(value: datetime | None) -> str:
    return value.astimezone().strftime("%d.%m.%Y %H:%M:%S") if value else EMPTY


def scalar(value: object) -> str:
    if value is None:
        return EMPTY
    if isinstance(value, bool):
        return "Да" if value else "Нет"
    if isinstance(value, float):
        return f"{value:.6g}"
    return str(value)


def measured(value: float | int | None, unit: str, *, scale: float = 1) -> str:
    return f"{value / scale:.1f} {unit}" if value is not None else "Нет данных"


def section_title(label: str) -> ft.Text:
    return ft.Text(label, size=17, weight=ft.FontWeight.W_600, color=TEXT)


def field(label: str, value: str, *, col: dict[str, int] | int = 12) -> ft.Column:
    return ft.Column(
        [
            ft.Text(label, color=MUTED, size=13),
            ft.Text(value, color=TEXT, size=14),
        ],
        spacing=4,
        col=col,
    )


def table(rows: list[tuple[str, ...]], *, header: tuple[str, ...] = ()) -> ft.Column:
    controls = []
    for index, cells in enumerate([header, *rows] if header else rows):
        heading = bool(header) and index == 0
        controls.append(
            ft.Container(
                ft.Row(
                    [
                        ft.Text(
                            value,
                            expand=True,
                            size=13,
                            color=MUTED if heading else TEXT,
                            weight=ft.FontWeight.W_600 if heading else None,
                        )
                        for value in cells
                    ],
                    spacing=10,
                    vertical_alignment=ft.CrossAxisAlignment.START,
                ),
                bgcolor=SUBTLE if heading else None,
                border=ft.Border.only(bottom=ft.BorderSide(1, LINE)),
                padding=ft.Padding.symmetric(horizontal=10, vertical=10),
            )
        )
    return ft.Column(
        controls,
        spacing=0,
        horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
    )


def scroll_lines(lines: list[str]) -> ft.Control:
    if not lines:
        return ft.Text("Нет записей.", color=MUTED)
    return ft.Container(
        ft.SelectionArea(
            content=ft.ListView(
                [ft.Text(line, size=13, color=TEXT) for line in lines],
                spacing=6,
                height=min(230, max(54, 32 * len(lines))),
            )
        ),
        bgcolor=SUBTLE,
        border_radius=CONTROL_RADIUS,
        padding=12,
    )


class SeriesPlot:
    def __init__(self, title: str, *, color: str = PRIMARY) -> None:
        self.width = 240.0
        self.points: list[tuple[datetime, float | None]] = []
        self.color = color
        self.unit = ""
        self.limits: tuple[float, float] | None = None
        self.graph = cv.Canvas(height=110, resize_interval=0, on_resize=self.resize)
        self.range = ft.Text(color=MUTED, size=12)
        self.content = ft.Container(
            ft.Column(
                [
                    ft.Text(
                        title,
                        color=color,
                        weight=ft.FontWeight.W_600,
                        max_lines=2,
                        overflow=ft.TextOverflow.ELLIPSIS,
                        tooltip=title,
                    ),
                    self.graph,
                    self.range,
                ],
                spacing=6,
                horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
            ),
            bgcolor=SURFACE,
            border=ft.Border.all(1, LINE),
            border_radius=CONTROL_RADIUS,
            padding=12,
            height=218,
            col={"xs": 12, "md": 6},
        )

    async def resize(self, event: cv.CanvasResizeEvent) -> None:
        if event.width > 12 and event.width != self.width:
            self.width = event.width
            self.render()
            self.content.update()

    def set_values(
        self,
        points: list[tuple[datetime, float | None]],
        *,
        unit: str = "",
        limits: tuple[float, float] | None = None,
    ) -> None:
        self.points, self.unit, self.limits = points, unit, limits
        self.render()

    def render(self) -> None:
        present = [value for _, value in self.points if value is not None and isfinite(value)]
        if not present:
            self.graph.shapes = []
            self.range.value = "Для графика пока нет данных."
            return

        low, high = self.limits or (min(present), max(present))
        scale = max(abs(low), abs(high)) if not isfinite(high - low) else 1
        scaled_low = low / scale
        spread = high / scale - scaled_low
        start, end = self.points[0][0], self.points[-1][0]
        span = (end - start).total_seconds()
        left, right, top, bottom = 5, self.width - 5, 5, 105
        guide = ft.Paint(color=LINE, stroke_width=1)
        line = ft.Paint(color=self.color, stroke_width=2.5)
        shapes = [cv.Line(left, y, right, y, guide) for y in (top, (top + bottom) / 2, bottom)]
        previous = None
        for when, value in self.points:
            if value is None or not isfinite(value):
                previous = None
                continue
            age = (when - start).total_seconds()
            x = left + (right - left) * (min(1, max(0, age / span)) if span > 0 else 0.5)
            y = bottom - (bottom - top) * ((value / scale - scaled_low) / spread if spread else 0.5)
            if previous is not None:
                shapes.append(cv.Line(*previous, x, y, line))
            shapes.append(cv.Circle(x, y, 2, line))
            previous = (x, y)
        self.graph.shapes = shapes
        axis = f"{scalar(low)}–{scalar(high)} {self.unit}".strip()
        self.range.value = (
            f"{start.astimezone():%H:%M:%S} — {end.astimezone():%H:%M:%S}"
            f" · Шкала {axis} · Точек {len(present)}"
        )


def history_chart(samples: list[MetricSample]) -> ft.Control:
    if not samples:
        return ft.Text("Нет отсчётов.", color=MUTED)
    grouped: dict[str, list[tuple[datetime, float | None]]] = {}
    for sample in sorted(samples, key=lambda sample: sample.timestamp):
        grouped.setdefault(sample.name, []).append((sample.timestamp, sample.value))
    cards = []
    for name, points in sorted(grouped.items()):
        plot = SeriesPlot(name)
        plot.set_values(points)
        cards.append(plot.content)
    return ft.ResponsiveRow(cards, spacing=12, run_spacing=12)


class TelemetryPanel:
    SERIES = {
        "cpu_percent": ("Загрузка ЦП", "%", 1, PRIMARY),
        "ram_used_bytes": ("Занято ОЗУ", "МиБ", MEBIBYTE, TEAL),
        "temperature_c": ("Температура", "°C", 1, AMBER),
        "network_rx_bytes": ("Принято по сети", "МиБ", MEBIBYTE, SUCCESS),
        "network_tx_bytes": ("Передано по сети", "МиБ", MEBIBYTE, PRIMARY),
    }

    def __init__(self, robot: RobotConnection) -> None:
        self.robot = robot
        self.task: asyncio.Task[None] | None = None
        self.samples: deque[SystemTelemetry] = deque(maxlen=60)
        self.status = ft.Text("Получаем телеметрию…", color=MUTED)
        self.readings = ft.ResponsiveRow(
            spacing=12,
            run_spacing=12,
            breakpoints={"xs": 0, "sm": 300, "md": 670},
            visible=False,
        )
        self.reading_values: list[tuple[ft.Text, ft.Text]] = []
        for label in ("Загрузка ЦП", "ОЗУ", "Температура", "Сеть"):
            value = ft.Text(
                size=17,
                weight=ft.FontWeight.W_700,
                max_lines=1,
                overflow=ft.TextOverflow.ELLIPSIS,
            )
            unit = ft.Text(color=MUTED, size=12)
            self.reading_values.append((value, unit))
            self.readings.controls.append(
                ft.Container(
                    ft.Column([ft.Text(label, color=MUTED, size=13), value, unit], spacing=2),
                    col={"xs": 12, "sm": 6, "md": 3},
                    height=95,
                    border_radius=CONTROL_RADIUS,
                    padding=12,
                )
            )
        self.connections = ft.ResponsiveRow(spacing=16, run_spacing=14)
        self.system = ft.Column(
            [section_title("Система и сеть"), self.connections],
            horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
            spacing=14,
            visible=False,
        )
        self.plots = {
            key: SeriesPlot(label, color=color) for key, (label, _, _, color) in self.SERIES.items()
        }
        self.chart_grid = ft.ResponsiveRow(
            [plot.content for plot in self.plots.values()], spacing=12, run_spacing=12
        )
        self.chart = ft.Column(
            [section_title("Графики"), self.chart_grid],
            horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
            spacing=12,
            visible=False,
        )
        self.metrics = ft.Column(horizontal_alignment=ft.CrossAxisAlignment.STRETCH, visible=False)
        self.content = ft.Column(
            [self.status, self.readings, self.chart, self.system, self.metrics],
            horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
            spacing=22,
        )

    def start(self) -> None:
        if self.task is None:
            self.task = asyncio.create_task(self.poll())

    def close(self) -> asyncio.Task[None] | None:
        task, self.task = self.task, None
        if task is not None:
            task.cancel()
        return task

    async def poll(self) -> None:
        async with self.robot.http_client() as http:
            while True:
                try:
                    sample = await self.robot.telemetry(http)
                except RobotError as exc:
                    self.status.value = str(exc)
                    self.status.color = ERROR
                    self.content.update()
                    if not exc.retryable:
                        return
                else:
                    self.samples.append(sample)
                    self.render(sample)
                    self.content.update()
                await asyncio.sleep(1)

    def render(self, sample: SystemTelemetry) -> None:
        ram = "Нет данных"
        ram_unit = ""
        if sample.ram_used_bytes is not None and sample.ram_total_bytes is not None:
            ram = (
                f"{sample.ram_used_bytes / MEBIBYTE:.0f} / {sample.ram_total_bytes / MEBIBYTE:.0f}"
            )
            ram_unit = "МиБ"
        network, network_color, network_tint = {
            True: ("Активна", SUCCESS, SUCCESS_TINT),
            False: ("Неактивна", ERROR, ERROR_TINT),
            None: ("Нет данных", MUTED, SUBTLE),
        }[sample.network_operational]
        self.status.value = f"Обновлено {sample.timestamp.astimezone():%H:%M:%S}"
        self.status.color = MUTED
        readings = [
            (measured(sample.cpu_percent, "%"), "", PRIMARY, PRIMARY_TINT),
            (ram, ram_unit, TEAL, TEAL_TINT),
            (measured(sample.temperature_c, "°C"), "", AMBER, AMBER_TINT),
            (network, "", network_color, network_tint),
        ]
        connections = [
            ("Сетевые интерфейсы", ", ".join(sample.interfaces) or "Нет данных"),
            ("Принято с запуска ОС", measured(sample.network_rx_bytes, "МиБ", scale=MEBIBYTE)),
            ("Передано с запуска ОС", measured(sample.network_tx_bytes, "МиБ", scale=MEBIBYTE)),
            ("Режим агента", "Yahboom" if sample.robot == "yahboom" else "Mock Robot"),
            ("Оборудование в RoboLab", "Занято" if sample.hardware_busy else "Свободно"),
            ("Камера", "Настроена" if sample.camera_available else "Не настроена"),
        ]
        for card, (value_text, unit_text), (value, unit, color, tint) in zip(
            self.readings.controls, self.reading_values, readings, strict=True
        ):
            value_text.value = value_text.tooltip = value
            value_text.color = color
            unit_text.value = unit
            unit_text.visible = bool(unit)
            card.bgcolor = tint
        self.connections.controls = [
            field(label, value, col={"xs": 6, "sm": 4}) for label, value in connections
        ]
        self.readings.visible = self.chart.visible = self.system.visible = self.metrics.visible = (
            True
        )

        self.metrics.controls = [
            section_title("Метрики скрипта"),
            *([ft.Text(str(sample.run_id), color=MUTED, size=12)] if sample.run_id else []),
            (
                table([(name, scalar(value)) for name, value in sorted(sample.metrics.items())])
                if sample.metrics
                else ft.Text("Метрик пока нет.", color=MUTED)
            ),
        ]
        self.render_graphs(sample.run_id)

    def render_graphs(self, run_id: UUID | None) -> None:
        for key, (_, unit, scale, _) in self.SERIES.items():
            points = [
                (
                    sample.timestamp,
                    None if (value := getattr(sample, key)) is None else value / scale,
                )
                for sample in self.samples
            ]
            self.plots[key].set_values(
                points, unit=unit, limits=(0, 100) if key == "cpu_percent" else None
            )

        names = {
            name for sample in self.samples if sample.run_id == run_id for name in sample.metrics
        }
        metric_keys = {f"metric:{name}" for name in names}
        if metric_keys != self.plots.keys() - self.SERIES.keys():
            for key in self.plots.keys() - self.SERIES.keys() - metric_keys:
                del self.plots[key]
            for name in sorted(names):
                key = f"metric:{name}"
                if key not in self.plots:
                    self.plots[key] = SeriesPlot(name, color=TEAL)
            self.chart_grid.controls = [
                self.plots[key].content for key in (*self.SERIES, *sorted(metric_keys))
            ]
        for name in names:
            points = [
                (sample.timestamp, sample.metrics.get(name) if sample.run_id == run_id else None)
                for sample in self.samples
            ]
            self.plots[f"metric:{name}"].set_values(points)


class ExperimentsPanel:
    def __init__(self, page: ft.Page, robot: RobotConnection, picker: ft.FilePicker) -> None:
        self.page = page
        self.robot = robot
        self.picker = picker
        self.closed = False
        self.task: asyncio.Task[None] | None = None
        self.page_items: list[Experiment] = []
        self.offset = 0
        self.has_more = False
        self.selected: dict[UUID, Experiment] = {}
        self.checkboxes: dict[UUID, ft.Checkbox] = {}
        self.detail_id: UUID | None = None
        self.loading = ft.ProgressBar(visible=False, color=PRIMARY)
        self.notice = ft.Text(visible=False)
        self.body = ft.Column(horizontal_alignment=ft.CrossAxisAlignment.STRETCH, spacing=14)
        self.selection_count = ft.Text(color=MUTED)
        self.compare_button = ft.FilledButton(
            "Сравнить", height=44, on_click=self.compare, disabled=True
        )
        self.clear_button = ft.OutlinedButton(
            "Сбросить выбор", height=44, on_click=self.clear, disabled=True
        )
        self.content = ft.Column(
            [self.loading, self.notice, self.body],
            horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
            spacing=14,
        )
        self.render_list()

    def start(self) -> None:
        self.submit(lambda: self.load(0))

    def close(self) -> asyncio.Task[None] | None:
        self.closed = True
        task, self.task = self.task, None
        if task is not None:
            task.cancel()
        return task

    def set_notice(self, message: str, *, error: bool = False) -> None:
        self.notice.value = message
        self.notice.color = ERROR if error else SUCCESS
        self.notice.visible = bool(message)

    def submit(self, action: Callable[[], Awaitable[None]]) -> None:
        if self.closed or self.task is not None:
            return
        self.set_notice("")
        self.loading.visible = self.body.disabled = True
        self.task = asyncio.create_task(self.perform(action))
        self.content.update()

    async def perform(self, action: Callable[[], Awaitable[None]]) -> None:
        try:
            await action()
        except RobotError as exc:
            self.set_notice(str(exc), error=True)
        finally:
            if not self.closed:
                self.task = None
                self.loading.visible = self.body.disabled = False
                self.content.update()

    async def load(self, offset: int) -> None:
        rows = await self.robot.history(limit=HISTORY_PAGE + 1, offset=offset)
        self.page_items, self.offset = rows[:HISTORY_PAGE], offset
        self.has_more = len(rows) > HISTORY_PAGE
        for run in self.page_items:
            if run.id in self.selected:
                self.selected[run.id] = run
        self.render_list()

    async def go_to_page(self, event: ft.ControlEvent) -> None:
        self.submit(lambda: self.load(event.control.data))

    def render_list(self) -> None:
        self.detail_id = None
        self.checkboxes.clear()
        rows = []
        for run in self.page_items:
            checkbox = ft.Checkbox(label="Для сравнения", on_change=self.choose, data=run)
            self.checkboxes[run.id] = checkbox
            rows.append(
                ft.Container(
                    border=ft.Border.only(bottom=ft.BorderSide(1, LINE)),
                    padding=ft.Padding.symmetric(vertical=14),
                    content=ft.ResponsiveRow(
                        [
                            ft.Column(
                                [
                                    section_title(run.script_name),
                                    ft.Text(
                                        f"{timestamp(run.started_at)} · {RUN_STATUS[run.status]} · "
                                        f"{run.elapsed_seconds:.1f} с",
                                        size=13,
                                        color=MUTED,
                                    ),
                                ],
                                spacing=5,
                                col={"xs": 12, "md": 7},
                            ),
                            ft.Container(
                                button_row(
                                    ft.OutlinedButton(
                                        "Открыть",
                                        height=44,
                                        tooltip=f"Открыть эксперимент «{run.script_name}»",
                                        data=run.id,
                                        on_click=self.open,
                                    ),
                                    checkbox,
                                ),
                                col={"xs": 12, "md": 5},
                            ),
                        ],
                        spacing=16,
                        run_spacing=4,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                )
            )
        self.body.controls = [
            self.selection_count,
            button_row(self.compare_button, self.clear_button),
            *(
                [ft.Column(rows, spacing=0, horizontal_alignment=ft.CrossAxisAlignment.STRETCH)]
                if rows
                else [
                    ft.Container(
                        ft.Text("На этой странице нет экспериментов.", color=MUTED),
                        bgcolor=SUBTLE,
                        border_radius=CONTROL_RADIUS,
                        padding=16,
                    )
                ]
            ),
            ft.Text(
                f"Страница {self.offset // HISTORY_PAGE + 1}",
                color=MUTED,
                text_align=ft.TextAlign.CENTER,
            ),
            button_row(
                ft.OutlinedButton(
                    "Назад",
                    height=44,
                    tooltip="Предыдущая страница",
                    data=max(0, self.offset - HISTORY_PAGE),
                    on_click=self.go_to_page,
                    disabled=self.offset == 0,
                ),
                ft.OutlinedButton(
                    "Далее",
                    height=44,
                    tooltip="Следующая страница",
                    data=self.offset + HISTORY_PAGE,
                    on_click=self.go_to_page,
                    disabled=not self.has_more,
                ),
                ft.OutlinedButton(
                    "Обновить", height=44, data=self.offset, on_click=self.go_to_page
                ),
            ),
        ]
        self.update_selection()

    def update_selection(self) -> None:
        count = len(self.selected)
        self.selection_count.value = (
            f"Выбрано {count} из 2" if count else "Выберите два эксперимента"
        )
        self.compare_button.disabled = count != 2
        self.clear_button.disabled = count == 0
        for run_id, checkbox in self.checkboxes.items():
            checkbox.value = run_id in self.selected
            checkbox.disabled = count == 2 and not checkbox.value

    async def choose(self, event: ft.ControlEvent) -> None:
        if self.closed or self.task is not None:
            return
        run = event.control.data
        if event.control.value and len(self.selected) < 2:
            self.selected[run.id] = run
        elif not event.control.value:
            self.selected.pop(run.id, None)
        self.update_selection()
        self.content.update()

    async def clear(self, event: ft.ControlEvent) -> None:
        if self.closed or self.task is not None:
            return
        self.selected.clear()
        self.update_selection()
        self.content.update()

    async def back(self, event: ft.ControlEvent) -> None:
        if self.closed or self.task is not None:
            return
        self.set_notice("")
        self.render_list()
        self.content.update()

    async def open(self, event: ft.ControlEvent) -> None:
        self.submit(lambda: self.load_detail(event.control.data))

    async def load_detail(self, run_id: UUID) -> None:
        details = await self.robot.experiment(run_id)
        self.detail_id = run_id
        if run_id in self.selected:
            self.selected[run_id] = details.run
        self.render_detail(details)

    def render_detail(self, details: ExperimentDetails) -> None:
        run = details.run
        items = [
            ("Состояние", RUN_STATUS[run.status]),
            ("Начало", timestamp(run.started_at)),
            ("Окончание", timestamp(run.finished_at)),
            ("Длительность", f"{run.elapsed_seconds:.2f} с"),
            ("Код выхода", scalar(run.exit_code)),
        ]
        logs = [
            f"{timestamp(line.timestamp)} · {line.level} · {line.source} · {line.message}"
            for line in details.logs
        ]
        samples = [
            f"{timestamp(sample.timestamp)} · {sample.name} · {scalar(sample.value)}"
            for sample in details.samples
        ]
        self.body.controls = [
            button_row(
                ft.TextButton("К списку", height=44, on_click=self.back),
                *[
                    ft.OutlinedButton(
                        format.upper(),
                        height=44,
                        tooltip=f"Сохранить {format.upper()}",
                        data=format,
                        on_click=self.save,
                    )
                    for format in ("json", "csv")
                ],
            ),
            ft.Text(run.script_name, size=22, weight=ft.FontWeight.W_600, color=TEXT),
            ft.Text(f"{run.script_id} · {run.id}", size=12, color=MUTED, selectable=True),
            ft.Container(
                ft.ResponsiveRow(
                    [field(label, value, col={"xs": 6, "md": 4}) for label, value in items],
                    spacing=14,
                    run_spacing=14,
                ),
                bgcolor=SUBTLE,
                border_radius=CONTROL_RADIUS,
                padding=14,
            ),
            section_title("Параметры"),
            self.mapping(run.parameters),
            section_title("Последние значения метрик"),
            self.mapping(run.metrics),
            section_title("Графики метрик"),
            history_chart(details.samples),
            section_title("Отсчёты"),
            scroll_lines(samples),
            section_title("Журнал эксперимента"),
            scroll_lines(logs),
        ]

    @staticmethod
    def mapping(values: dict) -> ft.Control:
        if not values:
            return ft.Text("Нет записей.", color=MUTED)
        return table([(name, scalar(value)) for name, value in sorted(values.items())])

    async def compare(self, event: ft.ControlEvent) -> None:
        if self.closed or self.task is not None or len(self.selected) != 2:
            return
        first, second = self.selected.values()
        fields = [
            ("Скрипт", first.script_name, second.script_name),
            ("Начало", timestamp(first.started_at), timestamp(second.started_at)),
            ("Длительность", f"{first.elapsed_seconds:.2f} с", f"{second.elapsed_seconds:.2f} с"),
            ("Состояние", RUN_STATUS[first.status], RUN_STATUS[second.status]),
        ]
        parameters = self.comparison(first.parameters, second.parameters)
        metrics = self.comparison(first.metrics, second.metrics)
        self.set_notice("")
        self.body.controls = [
            button_row(ft.TextButton("К списку", height=44, on_click=self.back)),
            ft.Text("Сравнение экспериментов", size=22, weight=ft.FontWeight.W_600, color=TEXT),
            table(fields, header=("Запуск", "Первый", "Второй")),
            section_title("Параметры"),
            table(parameters, header=("Параметр", "Первый", "Второй"))
            if parameters
            else ft.Text("Нет записей.", color=MUTED),
            section_title("Последние значения метрик"),
            table(metrics, header=("Показатель", "Первый", "Второй"))
            if metrics
            else ft.Text("Нет записей.", color=MUTED),
        ]
        self.content.update()

    @staticmethod
    def comparison(first: dict, second: dict) -> list[tuple[str, str, str]]:
        return [
            (key, scalar(first.get(key)), scalar(second.get(key)))
            for key in sorted(first.keys() | second.keys())
        ]

    async def save(self, event: ft.ControlEvent) -> None:
        run_id = self.detail_id
        if run_id is not None:
            self.submit(lambda: self.save_file(run_id, event.control.data))

    async def save_file(self, run_id: UUID, format: Literal["csv", "json"]) -> None:
        data = await self.robot.export_experiment(run_id, format)
        try:
            path = await self.picker.save_file(
                dialog_title="Сохранить эксперимент",
                file_name=f"robolab-{run_id}.{format}",
                src_bytes=data,
                file_type=ft.FilePickerFileType.CUSTOM,
                allowed_extensions=[format],
            )
        except (OSError, RuntimeError, TimeoutError, ValueError):
            self.set_notice(f"Не удалось сохранить {format.upper()}.", error=True)
        else:
            if path is not None or self.page.web:
                self.set_notice(
                    "Загрузка передана браузеру." if self.page.web else "Файл сохранён."
                )
