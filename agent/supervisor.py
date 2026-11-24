import asyncio
import json
import math
import os
import signal
import sys
from collections import deque
from collections.abc import Coroutine
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from uuid import UUID, uuid4

from agent.experiments import ExperimentStore
from agent.yahboom import Yahboom
from common.parameters import Value
from common.protocol import (
    LOG_LIMIT,
    EventFrame,
    Experiment,
    ExperimentDetails,
    LogLevel,
    LogLine,
    LogSource,
    MetricSample,
    RunStarted,
    RunState,
    RunStatus,
    SafetyState,
)

STOP_TIMEOUT = 3
HEARTBEAT_TIMEOUT = 4.0
EMERGENCY_TIMEOUT = 3.5
PROCESS_POLL = 0.05
LOG_LINE_LIMIT = 1000
MAX_METRIC_NAMES = 32
MAX_METRIC_SAMPLES = 1000


class RunConflict(Exception):
    pass


@dataclass
class Lease:
    token: UUID
    expires: float
    deadline: float


@dataclass
class Run:
    script_id: str
    script_name: str
    parameters: dict[str, Value]
    id: UUID = field(default_factory=uuid4)
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    started: float = field(default_factory=monotonic)
    process: asyncio.subprocess.Process | None = None
    task: asyncio.Task[None] | None = None
    finished: float | None = None
    finished_at: datetime | None = None
    status: RunStatus = "running"
    logs: deque[LogLine] = field(default_factory=lambda: deque(maxlen=LOG_LIMIT))
    sequence: int = 0
    metrics: dict[str, float] = field(default_factory=dict)
    samples: deque[MetricSample] = field(default_factory=lambda: deque(maxlen=MAX_METRIC_SAMPLES))
    device_fd: int | None = None
    lease: Lease | None = None
    watchdog: asyncio.Task[None] | None = None

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
    def __init__(
        self, hardware: Yahboom | None = None, history: ExperimentStore | None = None
    ) -> None:
        self.hardware = hardware
        self.history = history
        self.safety = SafetyState() if hardware is not None else None
        self.safety_generation = 0
        self.current: Run | None = None
        self.start_lock = asyncio.Lock()
        self.changed = asyncio.Event()
        self.closing = False

    def notify(self) -> None:
        changed, self.changed = self.changed, asyncio.Event()
        changed.set()

    async def start(
        self,
        script_id: str,
        path: Path,
        arguments: list[str],
        hardware_seconds: float | None = None,
        *,
        script_name: str | None = None,
        parameters: dict[str, Value] | None = None,
    ) -> RunStarted:
        async with self.start_lock:
            if self.closing or self.active_run:
                raise RunConflict("На агенте уже есть активный запуск.")

            device = self.hardware if hardware_seconds is not None else None
            if device is not None and self.safety is not None and self.safety.latched:
                raise RunConflict("Запуск заблокирован. Проверьте робота и снимите блокировку.")

            run = Run(script_id, script_name or script_id, parameters or {})
            device_options = {}
            environment = os.environ.copy()
            environment.pop("ROBOLAB_TOKEN", None)
            environment["PYTHONIOENCODING"] = "utf-8"
            executable = sys.executable
            process = None
            try:
                if device is not None:
                    run.device_fd = device.acquire()
                    await self.confirm_cutoff(run.device_fd)
                    if self.safety is not None and self.safety.latched:
                        raise RunConflict("Запуск прерван аварийной остановкой.")
                    executable = str(device.settings.python)
                    device_options = {"pass_fds": (run.device_fd,), "start_new_session": True}
                process = await asyncio.create_subprocess_exec(
                    executable,
                    "-u",
                    "-X",
                    "utf8",
                    str(path),
                    *arguments,
                    cwd=path.parent,
                    env=environment,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    **device_options,
                )
            except OSError:
                run.finished = monotonic()
                run.finished_at = datetime.now(UTC)
                run.status = "failed"
                self.current = run
                self.append_log(
                    run, "agent", "ERROR", "Не удалось подготовить или создать процесс."
                )
                self.record_run(run)
                raise
            finally:
                if run.device_fd is not None and process is None:
                    os.close(run.device_fd)
                    run.device_fd = None

            run.process = process
            self.current = run
            if hardware_seconds is not None and run.device_fd is not None:
                now = monotonic()
                run.lease = Lease(uuid4(), now + HEARTBEAT_TIMEOUT, now + hardware_seconds)
                self.update_cutoff(None)
                run.watchdog = asyncio.create_task(self.watch_lease(run))
            self.record_run(run)
            run.task = asyncio.create_task(self.collect(run, process))
            self.notify()
            return RunStarted(
                **run.state().model_dump(), lease=run.lease.token if run.lease else None
            )

    @property
    def active_run(self) -> Run | None:
        run = self.current
        return run if run is not None and run.finished is None else None

    def inhibit(self, reason: str) -> None:
        self.safety_generation += 1
        self.safety = SafetyState(latched=True, reason=reason)
        self.notify()

    def update_cutoff(self, confirmed: bool | None) -> None:
        assert self.safety is not None
        self.safety = self.safety.model_copy(update={"motor_disable_confirmed": confirmed})
        self.notify()

    async def confirm_cutoff(self, lock_fd: int) -> None:
        assert self.hardware is not None
        try:
            await self.hardware.motor_off(lock_fd)
        except OSError:
            self.inhibit("Не удалось подтвердить низкий уровень на входах ENA и ENB.")
            self.update_cutoff(False)
            raise
        self.update_cutoff(True)

    async def emergency_stop(self) -> SafetyState:
        assert self.hardware is not None
        self.inhibit("Запрошена аварийная остановка.")
        async with self.start_lock:
            run = self.active_run
            if run is not None and run.device_fd is not None and run.task is not None:
                self.stop(run.id, force=True)
                try:
                    await asyncio.wait_for(asyncio.shield(run.task), timeout=EMERGENCY_TIMEOUT)
                except TimeoutError as exc:
                    self.inhibit("Завершение процессов и проверка ENA/ENB ещё не подтверждены.")
                    self.update_cutoff(False)
                    raise OSError("Emergency cutoff timed out") from exc
                assert self.safety is not None
                if not self.safety.motor_disable_confirmed:
                    raise OSError("Motor enable cutoff not confirmed")
            else:
                await self.cutoff_with_lock()
            assert self.safety is not None
            return self.safety

    async def reset_safety(self) -> SafetyState:
        generation = self.safety_generation
        async with self.start_lock:
            if generation != self.safety_generation:
                raise RunConflict("До проверки запрошена аварийная остановка.")
            run = self.active_run
            if run is not None and run.device_fd is not None:
                raise RunConflict("Дождитесь завершения запуска.")
            await self.cutoff_with_lock()
            if generation != self.safety_generation:
                raise RunConflict("Во время проверки запрошена аварийная остановка.")
            self.safety = SafetyState(motor_disable_confirmed=True)
            self.notify()
            return self.safety

    async def cutoff_with_lock(self) -> None:
        assert self.hardware is not None
        fd = self.hardware.acquire()
        try:
            await self.confirm_cutoff(fd)
        finally:
            os.close(fd)

    def heartbeat(self, run_id: UUID, token: UUID) -> None:
        run = self.active_run
        now = monotonic()
        if (
            run is None
            or run.id != run_id
            or run.status != "running"
            or run.lease is None
            or run.lease.token != token
            or now >= min(run.lease.expires, run.lease.deadline)
        ):
            raise RunConflict("Подтверждение управления больше не действует.")
        run.lease.expires = now + HEARTBEAT_TIMEOUT

    async def watch_lease(self, run: Run) -> None:
        assert run.lease is not None and run.process is not None
        lease = run.lease
        while run.process.returncode is None:
            cutoff = (
                min(lease.deadline, lease.expires) if run.status == "running" else lease.deadline
            )
            remaining = cutoff - monotonic()
            if remaining > 0:
                await asyncio.sleep(remaining)
                continue
            if run.status == "running" and lease.expires <= lease.deadline:
                reason = "Потеряна связь с управляющим клиентом."
                self.inhibit(reason)
                self.append_log(run, "agent", "WARNING", reason)
            else:
                self.append_log(run, "agent", "INFO", "Истёк лимит времени запуска.")
            self.stop(run.id, force=True)
            return

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
                except (ValueError, RecursionError):
                    record = None
                if isinstance(record, dict):
                    self.add_metrics(run, record.get("metrics"))
                if isinstance(record, dict) and isinstance(record.get("message"), str):
                    message = record["message"].encode("utf-8", "replace").decode("utf-8")
                    candidate = record.get("level")
                    if candidate in ("DEBUG", "INFO", "WARNING", "ERROR"):
                        level = candidate
                    if isinstance(record.get("event"), str):
                        event = record["event"].encode("utf-8", "replace").decode("utf-8")
            self.append_log(run, source, level, message, event)

    def record_run(self, run: Run) -> None:
        if self.history is None:
            return
        state = run.state()
        self.history.record(
            ExperimentDetails(
                run=Experiment(
                    id=run.id,
                    script_id=run.script_id,
                    script_name=run.script_name,
                    parameters=run.parameters,
                    started_at=run.started_at,
                    finished_at=run.finished_at,
                    status=run.status,
                    elapsed_seconds=state.elapsed_seconds,
                    exit_code=state.exit_code,
                    metrics=run.metrics.copy(),
                ),
                logs=list(run.logs),
                samples=list(run.samples),
            )
        )

    @staticmethod
    def add_metrics(run: Run, metrics: object) -> None:
        if not isinstance(metrics, dict):
            return
        now = datetime.now(UTC)
        for name, value in metrics.items():
            if (
                not isinstance(name, str)
                or not 1 <= len(name) <= 64
                or not name.isprintable()
                or type(value) not in (int, float)
                or (name not in run.metrics and len(run.metrics) >= MAX_METRIC_NAMES)
            ):
                continue
            try:
                value = float(value)
            except OverflowError:
                continue
            if math.isfinite(value):
                run.metrics[name] = value
                run.samples.append(MetricSample(name=name, value=value, timestamp=now))

    async def collect(self, run: Run, process: asyncio.subprocess.Process) -> None:
        assert process.stdout is not None and process.stderr is not None
        stdout = self.drain(run, process.stdout, "stdout")
        stderr = self.drain(run, process.stderr, "stderr")
        if run.device_fd is None:
            await asyncio.gather(process.wait(), stdout, stderr)
            safe = True
        else:
            safe = await self.finish_hardware(run, process, stdout, stderr)

        run.finished = monotonic()
        run.finished_at = datetime.now(UTC)
        if not safe:
            run.status = "failed"
        elif run.status == "stopping":
            run.status = "stopped"
        else:
            run.status = "succeeded" if process.returncode == 0 else "failed"
        self.record_run(run)
        self.notify()

    async def finish_hardware(
        self,
        run: Run,
        process: asyncio.subprocess.Process,
        stdout: Coroutine[None, None, None],
        stderr: Coroutine[None, None, None],
    ) -> bool:
        assert run.device_fd is not None
        readers = asyncio.gather(stdout, stderr)
        safe = True
        try:
            try:
                await self.wait_for_exit(process)
                self.kill_group(process)
                await asyncio.wait_for(readers, timeout=1)
            except asyncio.CancelledError:
                safe = False
                self.kill_group(process)
                try:
                    await asyncio.wait_for(self.wait_for_exit(process), timeout=STOP_TIMEOUT)
                except TimeoutError:
                    pass
            except Exception:
                safe = False
                reason = "Не удалось завершить сбор вывода группы процессов."
                self.inhibit(reason)
                self.append_log(run, "agent", "ERROR", reason)
        finally:
            if run.watchdog is not None:
                run.watchdog.cancel()
            if not readers.done():
                readers.cancel()
            try:
                if process.returncode is None:
                    safe = False
                    self.inhibit("Процесс Yahboom ещё не завершён.")
                    self.update_cutoff(False)
                else:
                    try:
                        await self.confirm_cutoff(run.device_fd)
                    except OSError:
                        safe = False
                        self.append_log(
                            run, "agent", "ERROR", "Отключение моторов не подтверждено."
                        )
            finally:
                os.close(run.device_fd)
                run.device_fd = None
        return safe

    def stop(self, run_id: UUID, *, force: bool = False) -> RunState:
        run = self.current
        if run is None or run.id != run_id:
            raise KeyError(run_id)
        process = run.process
        if process is not None and process.returncode is None:
            if force and run.device_fd is not None:
                run.status = "stopping"
                self.kill_group(process)
                self.notify()
            elif run.status == "running":
                run.status = "stopping"
                self.notify()
                asyncio.create_task(self.terminate(run, process))
        return run.state()

    @staticmethod
    async def wait_for_exit(process: asyncio.subprocess.Process) -> None:
        while process.returncode is None:
            await asyncio.sleep(PROCESS_POLL)

    @staticmethod
    def kill_group(process: asyncio.subprocess.Process) -> None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    async def terminate(self, run: Run, process: asyncio.subprocess.Process) -> None:
        uses_hardware = run.device_fd is not None
        try:
            process.terminate()
            wait = self.wait_for_exit(process) if uses_hardware else process.wait()
            await asyncio.wait_for(wait, timeout=STOP_TIMEOUT)
        except ProcessLookupError:
            return
        except TimeoutError:
            self.append_log(run, "agent", "WARNING", "Процесс не ответил на сигнал остановки.")
            if uses_hardware:
                self.kill_group(process)
            else:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass

    def frame(self, run_id: UUID | None, after: int) -> EventFrame:
        run = self.current
        if run is None:
            return EventFrame(run=None, logs=[], safety=self.safety)
        if run_id != run.id:
            after = 0
        return EventFrame(
            run=run.state(),
            logs=[line for line in run.logs if line.sequence > after],
            gap=bool(run.logs and run.logs[0].sequence > after + 1),
            safety=self.safety,
        )

    async def close(self) -> None:
        async with self.start_lock:
            self.closing = True
            run = self.current
            if run is not None and run.task is not None:
                self.stop(run.id)
                try:
                    await asyncio.shield(run.task)
                except asyncio.CancelledError:
                    if run.device_fd is not None:
                        try:
                            await asyncio.wait_for(
                                asyncio.shield(run.task), timeout=STOP_TIMEOUT + EMERGENCY_TIMEOUT
                            )
                        except TimeoutError:
                            self.inhibit("Завершение Yahboom при остановке агента не подтверждено.")
                            self.update_cutoff(False)
                    else:
                        run.task.cancel()
                    raise
