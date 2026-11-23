import asyncio
from collections import deque
from collections.abc import Awaitable, Callable
from datetime import datetime
from math import isfinite
from typing import Literal
from uuid import UUID

import flet as ft
import flet.canvas as cv

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
MUTED = "#A7B7CB"
ACCENT = "#70E0CD"
ERROR = "#FFB4A9"
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


def field(label: str, value: str, *, col: dict[str, int] | int = 12) -> ft.Column:
    return ft.Column([ft.Text(label, color=MUTED, size=12), ft.Text(value)], spacing=1, col=col)


def table(rows: list[tuple[str, ...]]) -> ft.Column:
    return ft.Column(
        [
            ft.Row(
                [ft.Text(value, expand=True, size=13) for value in cells],
                vertical_alignment=ft.CrossAxisAlignment.START,
            )
            for cells in rows
        ],
        spacing=6,
    )


def scroll_lines(lines: list[str]) -> ft.Control:
    if not lines:
        return ft.Text("Нет записей.", color=MUTED)
    return ft.SelectionArea(
        content=ft.ListView(
            [ft.Text(line, size=13) for line in lines],
            spacing=5,
            height=min(230, 24 * len(lines) + 12),
        )
    )


class SeriesPlot:
    def __init__(self) -> None:
        self.width = 240.0
        self.points: list[tuple[datetime, float | None]] = []
        self.unit = ""
        self.limits: tuple[float, float] | None = None
        self.graph = cv.Canvas(height=110, on_resize=self.resize)
        self.range = ft.Text(color=MUTED, size=12)
        self.content = ft.Column(
            [self.graph, self.range], horizontal_alignment=ft.CrossAxisAlignment.STRETCH
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
        guide = ft.Paint(color="#43556B", stroke_width=1)
        line = ft.Paint(color=ACCENT, stroke_width=2.5)
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
    series = sorted(grouped)
    plot = SeriesPlot()
    plot.set_values(grouped[series[0]])

    async def select(event: ft.ControlEvent) -> None:
        plot.set_values(grouped[event.control.value])
        plot.content.update()

    return ft.Column(
        [
            ft.Dropdown(
                label="Ряд",
                value=series[0],
                options=[ft.DropdownOption(key=name, text=name) for name in series],
                on_select=select,
            ),
            plot.content,
        ],
        horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
    )


class TelemetryPanel:
    SERIES = {
        "cpu_percent": ("ЦП", "%", 1),
        "ram_used_bytes": ("Занято ОЗУ", "МиБ", MEBIBYTE),
        "temperature_c": ("Температура", "°C", 1),
        "network_rx_bytes": ("Принято по сети", "МиБ", MEBIBYTE),
        "network_tx_bytes": ("Передано по сети", "МиБ", MEBIBYTE),
    }

    def __init__(self, robot: RobotConnection) -> None:
        self.robot = robot
        self.task: asyncio.Task[None] | None = None
        self.samples: deque[SystemTelemetry] = deque(maxlen=60)
        self.status = ft.Text("Получаем телеметрию…", color=MUTED)
        self.readings = ft.ResponsiveRow(spacing=12, run_spacing=10, visible=False)
        self.series = ft.Dropdown(
            label="График",
            value="cpu_percent",
            options=[ft.DropdownOption(key=key, text=spec[0]) for key, spec in self.SERIES.items()],
            on_select=self.select_series,
        )
        self.plot = SeriesPlot()
        self.chart = ft.Column(
            [self.series, self.plot.content],
            horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
            visible=False,
        )
        self.metrics = ft.Column(visible=False)
        self.content = ft.Column(
            [self.status, self.readings, self.chart, self.metrics],
            horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
            spacing=14,
        )

    def start(self) -> None:
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
        if sample.ram_used_bytes is not None and sample.ram_total_bytes is not None:
            ram = f"{sample.ram_used_bytes / MEBIBYTE:.0f} / {sample.ram_total_bytes / MEBIBYTE:.0f} МиБ"
        link = {True: "Активны", False: "Неактивны", None: "Нет данных"}[sample.network_operational]
        if sample.interfaces:
            link = f"{link} · {', '.join(sample.interfaces)}"
        self.status.value = f"Обновлено {sample.timestamp.astimezone():%H:%M:%S}"
        self.status.color = MUTED
        fields = [
            ("Загрузка ЦП", measured(sample.cpu_percent, "%")),
            ("ОЗУ", ram),
            ("Температура", measured(sample.temperature_c, "°C")),
            ("Сетевые интерфейсы", link),
            ("Принято с запуска ОС", measured(sample.network_rx_bytes, "МиБ", scale=MEBIBYTE)),
            ("Передано с запуска ОС", measured(sample.network_tx_bytes, "МиБ", scale=MEBIBYTE)),
            ("Режим агента", "Yahboom" if sample.robot == "yahboom" else "Mock Robot"),
            ("Оборудование в RoboLab", "Занято" if sample.hardware_busy else "Свободно"),
            ("Камера", "Настроена" if sample.camera_available else "Не настроена"),
        ]
        self.readings.controls = [
            field(label, value, col={"xs": 6, "sm": 4, "lg": 3}) for label, value in fields
        ]
        self.readings.visible = self.chart.visible = self.metrics.visible = True

        metric_options = [
            ft.DropdownOption(key=f"metric:{name}", text=name) for name in sorted(sample.metrics)
        ]
        if [item.key for item in self.series.options[len(self.SERIES) :]] != [
            item.key for item in metric_options
        ]:
            self.series.options = self.series.options[: len(self.SERIES)] + metric_options
        if self.series.value not in {item.key for item in self.series.options}:
            self.series.value = "cpu_percent"
        self.metrics.controls = [
            ft.Text("Метрики скрипта", weight=ft.FontWeight.W_600),
            *([ft.Text(str(sample.run_id), color=MUTED, size=12)] if sample.run_id else []),
            (
                table([(name, scalar(value)) for name, value in sorted(sample.metrics.items())])
                if sample.metrics
                else ft.Text("Метрик пока нет.", color=MUTED)
            ),
        ]
        self.render_graph()

    async def select_series(self, event: ft.ControlEvent) -> None:
        if self.task is not None:
            self.render_graph()
            self.chart.update()

    def render_graph(self) -> None:
        key = self.series.value or "cpu_percent"
        unit = ""
        if key in self.SERIES:
            _, unit, scale = self.SERIES[key]
            raw = [getattr(sample, key) for sample in self.samples]
            values = [value / scale if value is not None else None for value in raw]
        elif self.samples:
            run_id = self.samples[-1].run_id
            name = key.removeprefix("metric:")
            values = [
                sample.metrics.get(name) if sample.run_id == run_id else None
                for sample in self.samples
            ]
        else:
            values = []
        self.plot.set_values(
            [(sample.timestamp, value) for sample, value in zip(self.samples, values, strict=True)],
            unit=unit,
            limits=(0, 100) if key == "cpu_percent" else None,
        )


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
        self.loading = ft.ProgressBar(visible=False)
        self.notice = ft.Text(visible=False)
        self.body = ft.Column(horizontal_alignment=ft.CrossAxisAlignment.STRETCH)
        self.selection_count = ft.Text(color=MUTED)
        self.compare_button = ft.FilledButton("Сравнить", on_click=self.compare, disabled=True)
        self.clear_button = ft.TextButton("Сбросить выбор", on_click=self.clear, disabled=True)
        self.content = ft.Column(
            [self.loading, self.notice, self.body],
            horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
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
        self.notice.color = ERROR if error else ACCENT
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
                    bgcolor="#1D2B3B",
                    border_radius=8,
                    padding=10,
                    content=ft.Column(
                        [
                            ft.Text(run.script_name, weight=ft.FontWeight.W_600),
                            ft.Text(
                                f"{timestamp(run.started_at)} · {RUN_STATUS[run.status]} · "
                                f"{run.elapsed_seconds:.1f} с",
                                size=13,
                                color=MUTED,
                            ),
                            ft.Row(
                                [
                                    ft.OutlinedButton(
                                        "Открыть",
                                        tooltip=f"Открыть эксперимент «{run.script_name}»",
                                        data=run.id,
                                        on_click=self.open,
                                    ),
                                    checkbox,
                                ],
                                wrap=True,
                            ),
                        ],
                        spacing=2,
                    ),
                )
            )
        self.body.controls = [
            ft.Row(
                [self.compare_button, self.clear_button, self.selection_count],
                wrap=True,
            ),
            *(
                [ft.ListView(rows, spacing=8, height=min(360, len(rows) * 132))]
                if rows
                else [ft.Text("На этой странице нет экспериментов.", color=MUTED)]
            ),
            ft.Row(
                [
                    ft.TextButton(
                        "Предыдущая страница",
                        data=max(0, self.offset - HISTORY_PAGE),
                        on_click=self.go_to_page,
                        disabled=self.offset == 0,
                    ),
                    ft.Text(f"Страница {self.offset // HISTORY_PAGE + 1}", color=MUTED),
                    ft.TextButton(
                        "Следующая страница",
                        data=self.offset + HISTORY_PAGE,
                        on_click=self.go_to_page,
                        disabled=not self.has_more,
                    ),
                    ft.OutlinedButton("Обновить", data=self.offset, on_click=self.go_to_page),
                ],
                wrap=True,
            ),
        ]
        self.update_selection()

    def update_selection(self) -> None:
        count = len(self.selected)
        self.selection_count.value = f"Выбрано {count} из 2"
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
            ft.Row(
                [
                    ft.TextButton("Назад к списку", on_click=self.back),
                    *[
                        ft.OutlinedButton(
                            f"Сохранить {format.upper()}", data=format, on_click=self.save
                        )
                        for format in ("json", "csv")
                    ],
                ],
                wrap=True,
            ),
            ft.Text(run.script_name, size=18, weight=ft.FontWeight.W_600),
            ft.Text(f"{run.script_id} · {run.id}", size=12, color=MUTED, selectable=True),
            ft.ResponsiveRow(
                [field(label, value, col={"xs": 6, "md": 4}) for label, value in items],
                run_spacing=10,
            ),
            ft.Text("Параметры", weight=ft.FontWeight.W_600),
            self.mapping(run.parameters),
            ft.Text("Последние значения метрик", weight=ft.FontWeight.W_600),
            self.mapping(run.metrics),
            ft.Text("График метрик", weight=ft.FontWeight.W_600),
            history_chart(details.samples),
            ft.Text("Отсчёты", weight=ft.FontWeight.W_600),
            scroll_lines(samples),
            ft.Text("Журнал эксперимента", weight=ft.FontWeight.W_600),
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
            ft.TextButton("Назад к списку", on_click=self.back),
            ft.Text("Сравнение экспериментов", size=18, weight=ft.FontWeight.W_600),
            table([("Запуск", "Первый", "Второй"), *fields]),
            ft.Text("Параметры", weight=ft.FontWeight.W_600),
            table(parameters) if parameters else ft.Text("Нет записей.", color=MUTED),
            ft.Text("Последние значения метрик", weight=ft.FontWeight.W_600),
            table(metrics) if metrics else ft.Text("Нет записей.", color=MUTED),
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
