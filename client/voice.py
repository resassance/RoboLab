import asyncio
import json
import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

VoiceCallback = Callable[[str], Awaitable[None]]
MODEL_NAME = "vosk-model-small-ru-0.22"


def _model_path() -> Path:
    configured = os.environ.get("ROBOLAB_VOICE_MODEL")
    model = Path(configured).expanduser() if configured else Path(__file__).with_name("voice_model")
    if not (model / "am" / "final.mdl").is_file():
        raise RuntimeError(
            f"Русская модель Vosk не найдена. Укажите путь к папке {MODEL_NAME} "
            "в ROBOLAB_VOICE_MODEL."
        )
    return model.resolve()


def _final_phrase(recognizer: Any, audio: bytes) -> str:
    if recognizer.AcceptWaveform(audio):
        return json.loads(recognizer.Result()).get("text", "").strip()
    return ""


def _open_desktop_microphone(model_path: Path, callback: Callable) -> tuple[Any, Any]:
    try:
        import sounddevice
        import vosk
    except (ImportError, OSError) as exc:
        raise RuntimeError("Для микрофона нужны Vosk, sounddevice и библиотека PortAudio.") from exc

    try:
        sample_rate = int(sounddevice.query_devices(kind="input")["default_samplerate"])
        model = vosk.Model(str(model_path))
        recognizer = vosk.KaldiRecognizer(model, sample_rate)
        stream = sounddevice.RawInputStream(
            samplerate=sample_rate,
            blocksize=sample_rate // 10,
            dtype="int16",
            channels=1,
            callback=callback,
        )
        try:
            stream.start()
        except Exception:
            stream.close()
            raise
    except Exception as exc:
        raise RuntimeError(f"Не удалось запустить микрофон и распознавание: {exc}") from exc
    return stream, recognizer


def _close_microphone(stream: Any) -> None:
    try:
        stream.abort()
    finally:
        stream.close()


class DesktopVoiceInput:
    def __init__(self, on_phrase: VoiceCallback, on_error: VoiceCallback) -> None:
        self.on_phrase = on_phrase
        self.on_error = on_error
        self._task: asyncio.Task | None = None
        self._pending: set[asyncio.Task] = set()
        self._stream: Any = None

    @property
    def active(self) -> bool:
        return self._task is not None

    async def start(self, model_path: Path) -> None:
        loop = asyncio.get_running_loop()
        chunks: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=8)
        error = None
        accepting = True

        def fail(message: str) -> None:
            nonlocal error
            if accepting and not error:
                error = message
                if not chunks.full():
                    chunks.put_nowait(None)

        def enqueue(chunk: bytes, status: str) -> None:
            if not accepting or error:
                return
            if status or chunks.full():
                fail("Прерван поток данных с микрофона.")
                return
            chunks.put_nowait(chunk)

        def dispatch(phrase: str) -> None:
            if len(self._pending) >= 8:
                fail("Распознавание остановлено: накопились необработанные команды.")
                return

            async def deliver() -> None:
                if accepting and not error:
                    await self.on_phrase(phrase)

            def finished(task: asyncio.Task) -> None:
                self._pending.discard(task)
                if not task.cancelled() and (exception := task.exception()) is not None:
                    fail(str(exception) or "Не удалось передать распознанную команду.")

            task = asyncio.create_task(deliver())
            self._pending.add(task)
            task.add_done_callback(finished)

        def capture(samples, frames, time_info, status) -> None:
            if accepting:
                loop.call_soon_threadsafe(enqueue, bytes(samples), str(status) if status else "")

        opening = asyncio.create_task(
            asyncio.to_thread(_open_desktop_microphone, model_path, capture)
        )
        try:
            self._stream, recognizer = await asyncio.shield(opening)
        except asyncio.CancelledError:
            accepting = False
            try:
                stream, _ = await opening
            except RuntimeError:
                pass
            else:
                await asyncio.to_thread(_close_microphone, stream)
            raise

        async def listen() -> None:
            nonlocal accepting
            try:
                while not error:
                    try:
                        chunk = await asyncio.wait_for(chunks.get(), timeout=1)
                    except TimeoutError as exc:
                        raise RuntimeError("Микрофон перестал передавать звук.") from exc
                    if chunk is None:
                        break
                    phrase = await asyncio.to_thread(_final_phrase, recognizer, chunk)
                    if phrase and not error:
                        dispatch(phrase)
                raise RuntimeError(error)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                accepting = False
                self._cancel_commands()
                await self._release()
                self._task = None
                await self.on_error(str(exc))
            finally:
                accepting = False

        self._task = asyncio.create_task(listen())

    def _cancel_commands(self) -> None:
        current = asyncio.current_task()
        for pending in self._pending:
            if pending is not current:
                pending.cancel()
        self._pending.clear()

    async def _release(self) -> None:
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                await asyncio.to_thread(_close_microphone, stream)
            except Exception:
                pass

    async def stop(self) -> None:
        self._cancel_commands()
        task, self._task = self._task, None
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await self._release()


class AndroidVoiceInput:
    def __init__(
        self, service_type: Any, on_phrase: VoiceCallback, on_error: VoiceCallback
    ) -> None:
        self.on_phrase = on_phrase
        self.on_error = on_error
        self.active = False
        self._session = 0
        self.control = service_type(on_phrase=self._receive, on_error=self._error)

    async def _receive(self, event: Any) -> None:
        if self.active and event.session == self._session:
            await self.on_phrase(event.text)

    async def _error(self, event: Any) -> None:
        if self.active and event.session == self._session:
            self.active = False
            await self.on_error(event.message)

    async def start(self, model_path: Path) -> None:
        self._session += 1
        self.active = True
        try:
            await self.control.start(str(model_path), self._session)
        except Exception as exc:
            await self.stop()
            raise RuntimeError(f"Не удалось запустить микрофон на Android: {exc}") from exc
        except asyncio.CancelledError:
            await self.stop()
            raise

    async def stop(self) -> None:
        self.active = False
        try:
            await self.control.stop(self._session)
        except Exception:
            pass


class VoiceInput:
    def __init__(self, page: Any, on_phrase: VoiceCallback, on_error: VoiceCallback) -> None:
        self.control = None
        self._lock = asyncio.Lock()
        self._backend: DesktopVoiceInput | AndroidVoiceInput | None = None
        self._unavailable = "На этой платформе локальное распознавание недоступно."
        platform_name = getattr(page.platform, "value", page.platform)
        if page.web:
            self._unavailable = "В браузере голосовое управление недоступно."
        elif platform_name in ("linux", "windows"):
            self._backend = DesktopVoiceInput(on_phrase, on_error)
        elif platform_name == "android":
            try:
                from robolab_voice_android import VoiceService
            except ImportError:
                self._unavailable = "Эта сборка Android не включает расширение микрофона."
            else:
                self._backend = AndroidVoiceInput(VoiceService, on_phrase, on_error)
                self.control = self._backend.control

    @property
    def active(self) -> bool:
        return self._backend is not None and self._backend.active

    async def start(self) -> None:
        async with self._lock:
            if self._backend is None:
                raise RuntimeError(self._unavailable)
            if not self.active:
                await self._backend.start(_model_path())

    async def stop(self) -> None:
        async with self._lock:
            if self._backend is not None and self.active:
                await self._backend.stop()
