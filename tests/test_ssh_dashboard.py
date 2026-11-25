import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import flet as ft

from client.main import Dashboard
from client.ssh import SshError, SshTarget


def test_agent_setup_failure_keeps_ssh_files_available(monkeypatch):
    async def check():
        disconnected = asyncio.Event()
        session = SimpleNamespace(
            closed=False,
            files=object(),
            wait_closed=disconnected.wait,
            prepare_agent=AsyncMock(side_effect=SshError("Не удалось подготовить агент.")),
            close_agent=AsyncMock(),
            close=AsyncMock(),
        )
        target = SshTarget("robot.local", 22, "pi")
        page = SimpleNamespace(
            web=False,
            width=1100,
            height=720,
            services=[],
            update=Mock(),
            add=Mock(),
            run_task=Mock(),
            platform=ft.PagePlatform.LINUX,
        )
        dashboard = Dashboard(page)
        dashboard.mount()
        assert dashboard.connection_view.visible
        assert not dashboard.workspace_home.visible
        assert not dashboard.mobile_navigation.visible
        login = SimpleNamespace(connect=AsyncMock(return_value=(target, session)))
        monkeypatch.setattr("client.main.confirm", AsyncMock(return_value=True))
        panel = SimpleNamespace(content=ft.Container(), start=Mock(), close=Mock(return_value=None))
        file_panel = Mock(return_value=panel)
        monkeypatch.setattr("client.main.FilePanel", file_panel)

        try:
            await dashboard.connect_ssh(login)
            assert dashboard.robot is None
            assert dashboard.connection_status.value == "SSH · подключено"
            assert dashboard.files_button.visible
            assert dashboard.workspace_home.visible
            assert not dashboard.connection_view.visible
            assert dashboard.desktop_navigation.visible
            page.width = 390
            await dashboard.resize(None)
            assert dashboard.mobile_navigation.visible
            assert not dashboard.desktop_navigation.visible
            await dashboard.open_files(None)
            assert file_panel.call_args.args[1] is session.files
            assert dashboard.files_view.visible and not dashboard.run_view.visible

            await dashboard.prepare_agent(None)
            assert dashboard.error.value == "Не удалось подготовить агент."
            assert dashboard.ssh_session is session
            assert dashboard.files is panel
            assert not dashboard.agent_button.disabled
        finally:
            await dashboard.disconnect()

        session.close.assert_awaited_once()

    asyncio.run(check())


def test_ready_camera_and_journal_share_run_view_and_pause_for_physical_run():
    from datetime import UTC, datetime
    from uuid import uuid4

    from common.protocol import EventFrame, RunStarted, SafetyState, Script

    async def check():
        robot = SimpleNamespace(
            kind="yahboom",
            camera_available=True,
            files_available=False,
            observability_available=False,
            start=AsyncMock(),
        )
        page = SimpleNamespace(
            web=False,
            width=390,
            height=844,
            services=[],
            update=Mock(),
            add=Mock(),
            run_task=Mock(),
            platform=ft.PagePlatform.LINUX,
        )
        dashboard = Dashboard(page)
        dashboard.mount()
        dashboard.robot, dashboard.connected = robot, True
        dashboard.safety = SafetyState()
        script = Script(
            id="distance", name="Дальномер", description="Стендовый запуск", requires_heartbeat=True
        )
        dashboard.scripts = {script.id: script}
        dashboard.selector.value = script.id
        dashboard.refresh()
        camera = dashboard.camera
        assert dashboard.camera_slot.visible
        assert dashboard.monitor_grid.controls == [dashboard.camera_slot, dashboard.journal_slot]
        assert dashboard.journal_slot.content is dashboard.journal.content

        start = RunStarted(
            id=uuid4(),
            script_id=script.id,
            status="running",
            pid=None,
            started_at=datetime.now(UTC),
            elapsed_seconds=0,
            exit_code=None,
            lease=uuid4(),
        )
        camera.pause_for_run = AsyncMock()

        async def start_after_camera(*args, **kwargs):
            camera.pause_for_run.assert_awaited_once()
            return start

        robot.start.side_effect = start_after_camera
        try:
            await dashboard.start(None)
            assert camera.start_button.disabled
            dashboard.show_frame(EventFrame(run=start, logs=[], safety=dashboard.safety))
            dashboard.refresh()
            assert not dashboard.selector.visible
            assert dashboard.journal.summary.controls
            assert camera.start_button.disabled
        finally:
            await dashboard.disconnect()

    asyncio.run(check())
