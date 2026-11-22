import asyncio
import json
import os
import sys
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from uuid import UUID, uuid4

from common.protocol import LOG_LIMIT, EventFrame, LogLevel, LogLine, LogSource, RunState, RunStatus

STOP_TIMEOUT = 3
LOG_LINE_LIMIT = 1000


class RunConflict(Exception):
    pass


@dataclass
class Run:
    script_id: str
    id: UUID = field(default_factory=uuid4)
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    started: float = field(default_factory=monotonic)
    process: asyncio.subprocess.Process | None = None
    task: asyncio.Task[None] | None = None
    finished: float | None = None
    status: RunStatus = "running"
    logs: deque[LogLine] = field(default_factory=lambda: deque(maxlen=LOG_LIMIT))
    sequence: int = 0

    def state(self) -> RunState:
        return RunState(
            id=self.id,
            script_id=self.script_id,
            status=self.status,
            pid=self.process.pid if self.process else None,
            started_at=self.started_at,
            elapsed_seconds=round(
                (self.finished if self.finished is not None else monotonic()) - self.started, 2
            ),
            exit_code=self.process.returncode
            if self.finished is not None and self.process
            else None,
        )


class Supervisor:
    def __init__(self) -> None:
        self.current: Run | None = None
        self.start_lock = asyncio.Lock()
        self.changed = asyncio.Event()
        self.closing = False

    def notify(self) -> None:
        changed, self.changed = self.changed, asyncio.Event()
        changed.set()

    async def start(self, script_id: str, path: Path) -> RunState:
        async with self.start_lock:
            if self.closing or (self.current is not None and self.current.finished is None):
                raise RunConflict

            run = Run(script_id)
            environment = os.environ.copy()
            environment.pop("ROBOLAB_TOKEN", None)
            try:
                process = await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-u",
                    "-X",
                    "utf8",
                    str(path),
                    cwd=path.parent,
                    env=environment,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            except OSError:
                run.finished = monotonic()
                run.status = "failed"
                self.current = run
                self.append_log(run, "agent", "ERROR", "Не удалось создать процесс.")
                raise

            run.process = process
            self.current = run
            run.task = asyncio.create_task(self.collect(run, process))
            self.notify()
            return run.state()

    def append_log(
        self, run: Run, source: LogSource, level: LogLevel, message: str, event: str | None = None
    ) -> None:
        run.sequence += 1
        run.logs.append(
            LogLine(
                sequence=run.sequence,
                timestamp=datetime.now(UTC),
                source=source,
                level=level,
                message=message[:LOG_LINE_LIMIT],
                event=event[:80] if event else None,
            )
        )
        self.notify()

    async def drain(self, run: Run, stream: asyncio.StreamReader, source: LogSource) -> None:
        while True:
            try:
                raw = await stream.readline()
            except ValueError:
                self.append_log(run, source, "WARNING", "Слишком длинная строка пропущена.")
                continue
            if not raw:
                return
            message = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            level: LogLevel = "WARNING" if source == "stderr" else "INFO"
            event = None
            if source == "stdout" and message.startswith("{") and len(message) <= 4096:
                try:
                    record = json.loads(message)
                except ValueError:
                    record = None
                if isinstance(record, dict) and isinstance(record.get("message"), str):
                    message = record["message"]
                    candidate = record.get("level")
                    if candidate in ("DEBUG", "INFO", "WARNING", "ERROR"):
                        level = candidate
                    if isinstance(record.get("event"), str):
                        event = record["event"]
            self.append_log(run, source, level, message, event)

    async def collect(self, run: Run, process: asyncio.subprocess.Process) -> None:
        assert process.stdout is not None and process.stderr is not None
        code, *_ = await asyncio.gather(
            process.wait(),
            self.drain(run, process.stdout, "stdout"),
            self.drain(run, process.stderr, "stderr"),
        )
        run.finished = monotonic()
        if run.status == "stopping":
            run.status = "stopped"
        else:
            run.status = "succeeded" if code == 0 else "failed"
        self.notify()

    def stop(self, run_id: UUID) -> RunState:
        run = self.current
        if run is None or run.id != run_id:
            raise KeyError(run_id)
        if run.status == "running" and run.process is not None and run.process.returncode is None:
            run.status = "stopping"
            self.notify()
            asyncio.create_task(self.terminate(run, run.process))
        return run.state()

    async def terminate(self, run: Run, process: asyncio.subprocess.Process) -> None:
        try:
            process.terminate()
            await asyncio.wait_for(process.wait(), timeout=STOP_TIMEOUT)
        except ProcessLookupError:
            return
        except TimeoutError:
            self.append_log(run, "agent", "WARNING", "Процесс не ответил на сигнал остановки.")
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.wait()

    def frame(self, run_id: UUID | None, after: int) -> EventFrame:
        run = self.current
        if run is None:
            return EventFrame(run=None, logs=[])
        if run_id != run.id:
            after = 0
        return EventFrame(
            run=run.state(),
            logs=[line for line in run.logs if line.sequence > after],
            gap=bool(run.logs and run.logs[0].sequence > after + 1),
        )

    async def close(self) -> None:
        async with self.start_lock:
            self.closing = True
            run = self.current
            if run is not None and run.task is not None:
                self.stop(run.id)
                await run.task
