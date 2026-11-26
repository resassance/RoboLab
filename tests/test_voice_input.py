import asyncio
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from client.voice import VoiceInput


@pytest.fixture()
def microphone(monkeypatch, tmp_path):
    model = tmp_path / "model"
    (model / "am").mkdir(parents=True)
    (model / "am" / "final.mdl").touch()
    monkeypatch.setenv("ROBOLAB_VOICE_MODEL", str(model))

    recognizer = Mock()
    recognizer.AcceptWaveform.side_effect = lambda data: data != b"partial"
    recognizer.Result.side_effect = ['{"text":"  не вперед  "}', '{"text":""}']
    vosk = SimpleNamespace(Model=Mock(), KaldiRecognizer=Mock(return_value=recognizer))
    stream = Mock()
    audio = SimpleNamespace(
        RawInputStream=Mock(return_value=stream),
        query_devices=Mock(return_value={"default_samplerate": 48000.0}),
    )
    monkeypatch.setitem(sys.modules, "vosk", vosk)
    monkeypatch.setitem(sys.modules, "sounddevice", audio)
    return audio, stream, vosk


def test_desktop_delivers_only_final_nonempty_phrases_and_stops(microphone):
    audio, stream, vosk = microphone

    async def run():
        received = []
        delivery = asyncio.Event()

        async def phrase(text):
            received.append(text)
            delivery.set()

        async def error(message):
            pytest.fail(message)

        voice = VoiceInput(SimpleNamespace(web=False, platform="linux"), phrase, error)
        await voice.start()
        assert voice.active
        callback = audio.RawInputStream.call_args.kwargs["callback"]
        audio_kwargs = audio.RawInputStream.call_args.kwargs
        assert audio_kwargs["channels"] == 1
        assert audio_kwargs["dtype"] == "int16"
        assert audio_kwargs["samplerate"] == 48000
        assert vosk.KaldiRecognizer.call_args.args[1] == 48000
        callback(b"partial", 1, None, False)
        callback(b"final", 1, None, False)
        await asyncio.wait_for(delivery.wait(), 1)
        callback(b"silence", 1, None, False)
        await voice.stop()

        assert received == ["не вперед"]
        assert not voice.active
        stream.abort.assert_called_once()
        stream.close.assert_called_once()

    asyncio.run(run())


def test_desktop_stops_and_reports_capture_failure(microphone):
    audio, stream, _ = microphone

    async def run():
        delivery = asyncio.Event()
        errors = []

        async def phrase(text):
            pytest.fail(text)

        async def error(message):
            errors.append(message)
            delivery.set()

        voice = VoiceInput(SimpleNamespace(web=False, platform="windows"), phrase, error)
        await voice.start()
        callback = audio.RawInputStream.call_args.kwargs["callback"]
        callback(b"", 1, None, "overflow")
        await asyncio.wait_for(delivery.wait(), 1)
        assert not voice.active
        assert "микрофон" in errors[0].lower()
        stream.close.assert_called_once()

    asyncio.run(run())


def test_browser_never_opens_host_microphone(microphone):
    audio, _, _ = microphone

    async def run():
        async def unexpected(_):
            pytest.fail("The host must not receive browser microphone events")

        voice = VoiceInput(SimpleNamespace(web=True, platform="linux"), unexpected, unexpected)
        with pytest.raises(RuntimeError, match="браузер"):
            await voice.start()
        assert not voice.active
        audio.RawInputStream.assert_not_called()

    asyncio.run(run())


def test_desktop_listens_for_stop_during_a_slow_command(microphone):
    audio, _, vosk = microphone
    recognizer = vosk.KaldiRecognizer.return_value
    recognizer.Result.side_effect = ['{"text":"вперед"}', '{"text":"стоп"}']

    async def run():
        moving = asyncio.Event()
        stopped = asyncio.Event()
        cancelled = asyncio.Event()

        async def phrase(text):
            if text == "стоп":
                stopped.set()
            else:
                moving.set()
                try:
                    await asyncio.Future()
                finally:
                    cancelled.set()

        async def error(message):
            pytest.fail(message)

        voice = VoiceInput(SimpleNamespace(web=False, platform="linux"), phrase, error)
        await voice.start()
        callback = audio.RawInputStream.call_args.kwargs["callback"]
        try:
            callback(b"drive", 1, None, False)
            await asyncio.wait_for(moving.wait(), 1)
            callback(b"stop", 1, None, False)
            await asyncio.wait_for(stopped.wait(), 1)
        finally:
            await voice.stop()
        await asyncio.wait_for(cancelled.wait(), 1)

    asyncio.run(run())


def test_android_discards_events_from_old_microphone_sessions(monkeypatch, microphone):
    def create_service(**callbacks):
        return SimpleNamespace(start=AsyncMock(), stop=AsyncMock(), **callbacks)

    monkeypatch.setitem(
        sys.modules, "robolab_voice_android", SimpleNamespace(VoiceService=create_service)
    )

    async def run():
        phrase = AsyncMock()
        error = AsyncMock()
        voice = VoiceInput(SimpleNamespace(web=False, platform="android"), phrase, error)
        service = voice.control
        assert service is not None
        await voice.start()
        first_session = service.start.call_args.args[1]

        await service.on_error(SimpleNamespace(session=first_session, message="отключён"))
        error.assert_awaited_once_with("отключён")
        assert not voice.active

        await voice.start()
        second_session = service.start.call_args.args[1]
        assert second_session != first_session
        await service.on_phrase(SimpleNamespace(session=first_session, text="вперед"))
        await service.on_phrase(SimpleNamespace(session=second_session, text="стоп"))
        await voice.stop()
        await service.on_phrase(SimpleNamespace(session=second_session, text="влево"))

        phrase.assert_awaited_once_with("стоп")
        service.stop.assert_awaited_once_with(second_session)

    asyncio.run(run())
