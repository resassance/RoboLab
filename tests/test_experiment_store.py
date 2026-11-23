import asyncio
import csv
import io
import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from agent.experiments import ExperimentStore, export_experiment
from common.protocol import Experiment, ExperimentDetails, LogLine, MetricSample


@pytest.fixture
def experiment():
    timestamp = datetime(2026, 11, 23, 12, tzinfo=UTC)
    return ExperimentDetails(
        run=Experiment(
            id=uuid4(),
            script_id="mock-robot",
            script_name="Тестовый запуск",
            parameters={"speed": 40, "label": "=1+1"},
            started_at=timestamp,
            status="running",
            elapsed_seconds=0.5,
            metrics={"distance": 1.25},
        ),
        logs=[
            LogLine(
                sequence=1,
                timestamp=timestamp,
                source="stdout",
                level="INFO",
                message="=1+1, движение\nзавершено",
                event="move",
            )
        ],
        samples=[MetricSample(name="distance", value=1.25, timestamp=timestamp)],
    )


def test_store_drains_pending_writes_and_recovers_active_run(tmp_path, experiment):
    async def check():
        path = tmp_path / "experiments.sqlite3"
        store = ExperimentStore(path)
        await store.open()
        store.record(experiment)
        await store.close()

        reopened = ExperimentStore(path)
        await reopened.open()
        try:
            details = await reopened.get(experiment.run.id)
            assert details is not None
            assert details.run.status == "interrupted"
            assert details.run.finished_at is None
            assert details.run.parameters == {"speed": 40, "label": "=1+1"}
            assert details.logs == experiment.logs
            assert details.samples == experiment.samples
        finally:
            await reopened.close()

    asyncio.run(check())


def test_store_lists_latest_snapshots_with_stable_pagination(experiment):
    async def check():
        store = ExperimentStore(None)
        await store.open()
        try:
            store.record(experiment)
            time = experiment.run.started_at + timedelta(seconds=1)
            finished = experiment.run.model_copy(
                update={"status": "succeeded", "finished_at": time, "exit_code": 0}
            )
            store.record(experiment.model_copy(update={"run": finished}))
            newer = experiment.run.model_copy(update={"id": uuid4(), "started_at": time})
            store.record(experiment.model_copy(update={"run": newer}))

            assert await store.list(1, 0) == [newer]
            assert await store.list(10, 1) == [finished]
            assert await store.get(uuid4()) is None
        finally:
            await store.close()

    asyncio.run(check())


def test_failed_update_leaves_the_previous_snapshot_readable(tmp_path, experiment, caplog):
    async def check():
        path = tmp_path / "experiments.sqlite3"
        store = ExperimentStore(path)
        await store.open()
        try:
            store.record(experiment)
            assert await store.get(experiment.run.id) == experiment
            with closing(sqlite3.connect(path)) as connection:
                connection.execute("""
                    CREATE TRIGGER fail_experiment_update BEFORE UPDATE ON experiments
                    BEGIN SELECT RAISE(ABORT, 'simulated write failure'); END
                """)

            stopped = experiment.run.model_copy(update={"status": "stopped"})
            store.record(experiment.model_copy(update={"run": stopped, "logs": []}))
            assert await store.get(experiment.run.id) == experiment
            assert "simulated write failure" in caplog.text

            newer = experiment.run.model_copy(update={"id": uuid4()})
            store.record(experiment.model_copy(update={"run": newer}))
            assert (await store.get(newer.id)).run == newer
        finally:
            await store.close()

    asyncio.run(check())


def test_json_export_carries_run_parameters_logs_and_samples(experiment):
    body, media_type = export_experiment(experiment, "json")
    export = json.loads(body)
    assert media_type == "application/json"
    assert export["run"]["parameters"] == {"speed": 40, "label": "=1+1"}
    assert export["run"]["script_name"] == "Тестовый запуск"
    assert export["logs"][0]["message"] == "=1+1, движение\nзавершено"
    assert export["samples"][0]["value"] == 1.25


def test_csv_export_carries_records_and_escapes_formula_text(experiment):
    body, media_type = export_experiment(experiment, "csv")
    rows = list(csv.DictReader(io.StringIO(body.decode("utf-8"))))
    assert media_type == "text/csv"
    assert any(row["name"] == "id" and row["value"] == str(experiment.run.id) for row in rows)
    assert any(
        row["kind"] == "parameter" and row["name"] == "speed" and row["value"] == "40"
        for row in rows
    )
    assert any(
        row["kind"] == "sample"
        and row["name"] == "distance"
        and row["value"] == "1.25"
        and row["timestamp"] == "2026-11-23T12:00:00Z"
        for row in rows
    )
    log = next(row for row in rows if row["kind"] == "log")
    assert log["message"] == "'=1+1, движение\nзавершено"
    assert log["sequence"] == "1"
    assert log["event"] == "move"
