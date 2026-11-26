from dataclasses import dataclass

import flet as ft


@dataclass
class PhraseEvent(ft.Event["VoiceService"]):
    session: int
    text: str


@dataclass
class VoiceErrorEvent(ft.Event["VoiceService"]):
    session: int
    message: str


@ft.control("RoboLabVoice")
class VoiceService(ft.Service):
    on_phrase: ft.EventHandler[PhraseEvent] | None = None
    on_error: ft.EventHandler[VoiceErrorEvent] | None = None

    async def start(self, model_path: str, session: int) -> None:
        await self._invoke_method(
            "start", {"model_path": model_path, "session": session}, timeout=60
        )

    async def stop(self, session: int) -> None:
        await self._invoke_method("stop", {"session": session}, timeout=2)
