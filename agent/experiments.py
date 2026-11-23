from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Literal
from uuid import UUID

from common.protocol import Experiment, ExperimentDetails

logger = logging.getLogger(__name__)


class ExperimentStore:
    def __init__(self, path: Path | None) -> None:
        self._path = path
        self._worker: ThreadPoolExecutor | None = None
        self._connection: sqlite3.Connection | None = None
        self._lifecycle = asyncio.Lock()

    async def open(self) -> None:
        async with self._lifecycle:
            if self._worker is not None:
                return
            worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="robolab-experiments")
            try:
                await asyncio.wrap_future(worker.submit(self._open_database))
            except BaseException:
                await self._close_worker(worker)
                raise
            self._worker = worker

    def _open_database(self) -> None:
        connection = sqlite3.connect(self._path if self._path is not None else ":memory:")
        self._connection = connection
        connection.row_factory = sqlite3.Row
        with connection:
            connection.execute("""
                CREATE TABLE IF NOT EXISTS experiments (
                    id TEXT PRIMARY KEY NOT NULL,
                    started_at REAL NOT NULL,
                    status TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    records TEXT NOT NULL
                )
            """)
            connection.execute("""
                CREATE INDEX IF NOT EXISTS experiments_order
                ON experiments (started_at DESC, id DESC)
            """)
            connection.execute("""
                UPDATE experiments SET status = 'interrupted'
                WHERE status IN ('running', 'stopping')
            """)

    def record(self, details: ExperimentDetails) -> None:
        worker = self._worker
        if worker is not None:
            worker.submit(self._record, details)

    def _record(self, details: ExperimentDetails) -> None:
        connection = self._connection
        assert connection is not None
        run = details.run
        try:
            with connection:
                connection.execute(
                    """
                    INSERT INTO experiments (id, started_at, status, summary, records)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT (id) DO UPDATE SET
                        started_at = excluded.started_at,
                        status = excluded.status,
                        summary = excluded.summary,
                        records = excluded.records
                    """,
                    (
                        str(run.id),
                        run.started_at.timestamp(),
                        run.status,
                        run.model_dump_json(exclude={"status"}),
                        details.model_dump_json(exclude={"run"}),
                    ),
                )
        except Exception:
            logger.exception("Could not record experiment %s", run.id)

    def _opened_worker(self) -> ThreadPoolExecutor:
        if self._worker is None:
            raise RuntimeError("The experiment store is closed")
        return self._worker

    async def list(self, limit: int, offset: int) -> list[Experiment]:
        return await asyncio.wrap_future(self._opened_worker().submit(self._list, limit, offset))

    def _list(self, limit: int, offset: int) -> list[Experiment]:
        connection = self._connection
        assert connection is not None
        rows = connection.execute(
            "SELECT summary, status FROM experiments ORDER BY started_at DESC, id DESC "
            "LIMIT ? OFFSET ?",
            (limit, offset),
        )
        return [self._summary(row) for row in rows]

    async def get(self, run_id: UUID) -> ExperimentDetails | None:
        return await asyncio.wrap_future(self._opened_worker().submit(self._get, run_id))

    def _get(self, run_id: UUID) -> ExperimentDetails | None:
        connection = self._connection
        assert connection is not None
        row = connection.execute(
            "SELECT summary, status, records FROM experiments WHERE id = ?", (str(run_id),)
        ).fetchone()
        if row is None:
            return None
        return ExperimentDetails(run=self._summary(row), **json.loads(row["records"]))

    @staticmethod
    def _summary(row: sqlite3.Row) -> Experiment:
        return Experiment(status=row["status"], **json.loads(row["summary"]))

    async def close(self) -> None:
        async with self._lifecycle:
            worker, self._worker = self._worker, None
            if worker is not None:
                await self._close_worker(worker)

    async def _close_worker(self, worker: ThreadPoolExecutor) -> None:
        closing = asyncio.wrap_future(worker.submit(self._close_database))
        try:
            await asyncio.shield(closing)
        finally:
            await asyncio.to_thread(worker.shutdown)

    def _close_database(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None


def _csv_value(value: object) -> object:
    if isinstance(value, str) and (
        value.startswith(("\t", "\n", "\r")) or value.lstrip().startswith(("=", "+", "-", "@"))
    ):
        return "'" + value
    return value


def export_experiment(
    details: ExperimentDetails, format: Literal["json", "csv"]
) -> tuple[bytes, str]:
    if format == "json":
        return details.model_dump_json(indent=2).encode("utf-8"), "application/json"

    data = details.model_dump(mode="json")
    run = data["run"]
    parameters = run.pop("parameters")
    metrics = run.pop("metrics")
    output = io.StringIO(newline="")
    writer = csv.DictWriter(
        output,
        fieldnames=(
            "kind",
            "timestamp",
            "name",
            "value",
            "sequence",
            "source",
            "level",
            "event",
            "message",
        ),
    )
    writer.writeheader()
    writer.writerows(
        {"kind": "run", "name": name, "value": _csv_value(value)} for name, value in run.items()
    )
    writer.writerows(
        {
            "kind": "parameter",
            "name": _csv_value(name),
            "value": json.dumps(value, ensure_ascii=False),
        }
        for name, value in parameters.items()
    )
    writer.writerows(
        {"kind": "metric", "name": _csv_value(name), "value": value}
        for name, value in metrics.items()
    )
    writer.writerows(
        {"kind": "log", **{key: _csv_value(value) for key, value in log.items()}}
        for log in data["logs"]
    )
    writer.writerows(
        {"kind": "sample", **sample, "name": _csv_value(sample["name"])}
        for sample in data["samples"]
    )
    return output.getvalue().encode("utf-8"), "text/csv"
