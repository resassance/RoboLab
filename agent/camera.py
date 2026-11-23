import asyncio
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from struct import unpack
from time import monotonic
from uuid import UUID, uuid4

from common.protocol import CameraOptions, CameraStarted

WORKER = Path(__file__).resolve().with_name("camera_worker.py")
MAX_JPEG_SIZE = 768 * 1024
VIEWER_TIMEOUT = 5.0
FIRST_FRAME_TIMEOUT = 10.0
FRAME_TIMEOUT = 3.0
FRAME_WAIT = 1.0
STOP_WAIT = 3.0

logger = logging.getLogger(__name__)


class CameraBusy(Exception):
    pass


class CameraMissing(Exception):
    pass


class CameraUnavailable(Exception):
    pass


@dataclass(frozen=True)
class CameraSource:
    python: str | Path
    device: str
    acquire: Callable[[], int] | None = None


@dataclass(frozen=True)
class CameraFrame:
    jpeg: bytes
    sequence: int
    received_at: float
    fps: float

    @property
    def age_ms(self) -> int:
        return max(0, int((monotonic() - self.received_at) * 1000))


@dataclass
class Capture:
    process: asyncio.subprocess.Process
    lock_fd: int | None
    viewer_until: float
    id: UUID = field(default_factory=uuid4)
    stop: asyncio.Event = field(default_factory=asyncio.Event)
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    latest: CameraFrame | None = None
    task: asyncio.Task[None] | None = None
    failed: bool = False

    def notify(self) -> None:
        changed, self.changed = self.changed, asyncio.Event()
        changed.set()


class CameraPreview:
    def __init__(self, source: CameraSource | None) -> None:
        self.source = source
        self.current: Capture | None = None
        self.lock = asyncio.Lock()
        self.closed = False

    async def start(self, options: CameraOptions, allowed: Callable[[], bool]) -> CameraStarted:
        async with self.lock:
            source = self.source
            if source is None or self.closed:
                raise CameraMissing
            if not allowed() or (
                self.current and self.current.task and not self.current.task.done()
            ):
                raise CameraBusy

            lock_fd = source.acquire() if source.acquire is not None else None
            environment = os.environ.copy()
            environment.pop("ROBOLAB_TOKEN", None)
            try:
                process = await asyncio.create_subprocess_exec(
                    source.python,
                    "-I",
                    "-u",
                    WORKER,
                    "--device",
                    source.device,
                    "--view",
                    options.view,
                    "--fps",
                    str(options.fps),
                    "--quality",
                    str(options.quality),
                    env=environment,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    **({"pass_fds": (lock_fd,)} if lock_fd is not None else {}),
                )
            except BaseException:
                if lock_fd is not None:
                    os.close(lock_fd)
                raise

            capture = Capture(process, lock_fd, monotonic() + VIEWER_TIMEOUT)
            capture.task = asyncio.create_task(self.run_capture(capture))
            self.current = capture
            return CameraStarted(id=capture.id)

    async def frame(self, camera_id: UUID, after: int) -> CameraFrame | None:
        capture = self.current
        if capture is None or capture.id != camera_id:
            raise CameraMissing
        if capture.failed:
            raise CameraUnavailable
        if capture.stop.is_set() or monotonic() >= capture.viewer_until:
            raise CameraMissing

        capture.viewer_until = monotonic() + VIEWER_TIMEOUT
        change = capture.changed
        latest = capture.latest
        if latest is not None and latest.sequence > after:
            return latest

        try:
            await asyncio.wait_for(change.wait(), timeout=FRAME_WAIT)
        except TimeoutError:
            pass
        if capture.failed:
            raise CameraUnavailable
        if capture.stop.is_set():
            raise CameraMissing
        latest = capture.latest
        return latest if latest is not None and latest.sequence > after else None

    async def stop(self, camera_id: UUID | None = None) -> None:
        async with self.lock:
            capture = self.current
            if camera_id is not None and (capture is None or capture.id != camera_id):
                raise CameraMissing
            if capture is None:
                return
            capture.stop.set()
            capture.latest = None
            capture.notify()

        assert capture.task is not None
        try:
            await asyncio.wait_for(asyncio.shield(capture.task), timeout=STOP_WAIT)
        except TimeoutError as exc:
            capture.failed = True
            raise CameraUnavailable("Camera worker has not exited") from exc
        capture.failed = False

    async def close(self) -> None:
        self.closed = True
        try:
            await self.stop()
        except CameraUnavailable:
            logger.warning("Camera worker did not exit within the shutdown deadline")

    async def run_capture(self, capture: Capture) -> None:
        read = asyncio.create_task(self.read_frames(capture))
        expire = asyncio.create_task(self.wait_for_idle(capture))
        stop = asyncio.create_task(capture.stop.wait())
        tasks = (read, expire, stop)
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            if read in done and not capture.stop.is_set():
                try:
                    read.result()
                except (OSError, EOFError, ValueError) as exc:
                    logger.warning("Camera preview failed: %s", exc)
                    capture.failed = True
        finally:
            capture.stop.set()
            capture.latest = None
            capture.notify()
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            try:
                await self.finish_process(capture.process)
            finally:
                if capture.lock_fd is not None:
                    os.close(capture.lock_fd)

    async def read_frames(self, capture: Capture) -> None:
        stream = capture.process.stdout
        assert stream is not None
        while not capture.stop.is_set():
            previous = capture.latest
            timeout = FRAME_TIMEOUT if previous is not None else FIRST_FRAME_TIMEOUT
            async with asyncio.timeout(timeout):
                size = unpack("!I", await stream.readexactly(4))[0]
                if not 4 <= size <= MAX_JPEG_SIZE:
                    raise ValueError("invalid JPEG length")
                jpeg = await stream.readexactly(size)
            if not jpeg.startswith(b"\xff\xd8") or not jpeg.endswith(b"\xff\xd9"):
                raise ValueError("invalid JPEG markers")
            if capture.stop.is_set():
                return
            now = monotonic()
            fps = 1 / max(0.001, now - previous.received_at) if previous else 0.0
            turn = previous.sequence + 1 if previous else 1
            capture.latest = CameraFrame(jpeg, turn, now, fps)
            capture.notify()

    @staticmethod
    async def wait_for_idle(capture: Capture) -> None:
        while (remaining := capture.viewer_until - monotonic()) > 0:
            await asyncio.sleep(remaining)

    @staticmethod
    async def finish_process(process: asyncio.subprocess.Process) -> None:
        if process.returncode is None:
            try:
                process.terminate()
            except ProcessLookupError:
                pass
        try:
            await asyncio.wait_for(process.communicate(), timeout=1)
        except TimeoutError:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.communicate()
