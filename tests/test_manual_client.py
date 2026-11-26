import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

from client.manual import ManualPanel, spoken_command


def test_spoken_command_requires_an_exact_command():
    for phrase, command in [
        (" ВПЕРЁД ", "forward"),
        ("вперед", "forward"),
        ("назад", "backward"),
        ("влево", "left"),
        ("вправо", "right"),
        ("Стоп", "stop"),
        ("не вперёд", None),
        ("стоп и налево", None),
        ("[unk] вправо", None),
    ]:
        assert spoken_command(phrase) == command


def test_stop_during_pending_start_stops_the_response_without_heartbeat():
    async def check():
        pending = asyncio.Event()
        release = asyncio.Event()
        run = SimpleNamespace(id=uuid4(), lease=uuid4(), status="running")

        async def launch(direction):
            pending.set()
            await release.wait()
            return run

        robot = SimpleNamespace(start_manual=launch, stop_manual=AsyncMock(), heartbeat=AsyncMock())
        page = SimpleNamespace(web=True)
        panel = ManualPanel(page, lambda: robot, lambda: None)
        start = asyncio.create_task(panel.command("forward"))
        await pending.wait()
        stop = asyncio.create_task(panel.command("stop"))
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(start, stop)
        robot.stop_manual.assert_awaited_once_with(run.id, run.lease)
        robot.heartbeat.assert_not_awaited()

    asyncio.run(check())
