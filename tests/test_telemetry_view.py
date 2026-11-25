from datetime import UTC, datetime, timedelta
from uuid import uuid4

from client.experiments import TelemetryPanel, history_chart
from common.protocol import MetricSample, SystemTelemetry


def test_telemetry_shows_equal_metric_cards_and_all_live_charts():
    panel = TelemetryPanel(None)
    sample = SystemTelemetry(
        timestamp=datetime(2026, 11, 24, tzinfo=UTC),
        robot="yahboom",
        cpu_percent=15,
        ram_used_bytes=5 * 2**30,
        ram_total_bytes=16 * 2**30,
        network_operational=True,
        run_id=uuid4(),
        metrics={"distance": 12.5, "angle": 40},
    )

    panel.samples.append(sample)
    panel.render(sample)

    assert len(panel.readings.controls) == 4
    assert len({card.height for card in panel.readings.controls}) == 1
    assert all(card.col == panel.readings.controls[0].col for card in panel.readings.controls)
    assert len(panel.chart_grid.controls) == 7
    assert panel.plots["ram_used_bytes"].points == [(sample.timestamp, 5120)]
    assert panel.plots["temperature_c"].range.value == "Для графика пока нет данных."
    assert panel.plots["metric:distance"].points == [(sample.timestamp, 12.5)]
    assert panel.plots["metric:angle"].points == [(sample.timestamp, 40)]


def test_telemetry_chart_history_is_reused_without_mixing_experiments():
    panel = TelemetryPanel(None)
    now = datetime(2026, 11, 24, tzinfo=UTC)
    sample = SystemTelemetry(
        timestamp=now, robot="mock", run_id=uuid4(), metrics={"speed": 2, "angle": 4}
    )
    panel.samples.append(sample)
    panel.render(sample)
    speed_plot = panel.plots["metric:speed"]

    next_sample = sample.model_copy(update={"timestamp": now + timedelta(seconds=1)})
    panel.samples.append(next_sample)
    panel.render(next_sample)
    assert panel.plots["metric:speed"] is speed_plot
    assert speed_plot.points == [(now, 2), (next_sample.timestamp, 2)]

    other_run = next_sample.model_copy(
        update={
            "timestamp": now + timedelta(seconds=2),
            "run_id": uuid4(),
            "metrics": {"speed": 7},
        }
    )
    panel.samples.append(other_run)
    panel.render(other_run)
    assert "metric:angle" not in panel.plots
    assert panel.plots["metric:speed"].points == [
        (now, None),
        (next_sample.timestamp, None),
        (other_run.timestamp, 7),
    ]


def test_experiment_history_exposes_every_series_on_one_page():
    now = datetime(2026, 11, 24, tzinfo=UTC)
    graph = history_chart(
        [
            MetricSample(timestamp=now, name="speed", value=2),
            MetricSample(timestamp=now, name="angle", value=4),
        ]
    )
    assert [card.content.controls[0].value for card in graph.controls] == ["angle", "speed"]
