import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import flet as ft

from client.connection import CameraFrame


def test_camera_actions_fill_one_row_at_phone_width():
    from client.camera import CameraPanel

    panel = CameraPanel(SimpleNamespace(width=320), Mock(), Mock(), Mock())
    row = next(
        control
        for control in panel.content.content.controls
        if isinstance(control, ft.Row) and panel.start_button in control.controls
    )

    assert not row.wrap
    assert row.controls == [panel.start_button, panel.stop_button, panel.save_button]
    assert [button.expand for button in row.controls] == [1, 1, 1]

    panel.set_compact(False)
    wide_height = panel.preview.height
    panel.set_compact(True)
    assert panel.preview.height < wide_height


def test_stop_keeps_last_frame_and_allows_saving_jpeg():
    from client.camera import CameraPanel

    async def check():
        session = uuid4()
        started = asyncio.Event()
        streaming = asyncio.Event()
        frame = CameraFrame(b"jpeg-fixture", 12, 3, 0, 0)

        async def pull(_session, _fps):
            panel.show_frame(frame)
            started.set()
            await streaming.wait()

        robot = AsyncMock()
        robot.start_camera.return_value = session
        picker = AsyncMock()
        picker.save_file.return_value = "camera.jpg"
        released = Mock()
        panel = CameraPanel(SimpleNamespace(web=False), robot, picker, released)
        panel.pull = pull

        await panel.start(None)
        await started.wait()
        panel.set_run_active(True)
        await panel.pause_for_run()

        robot.stop_camera.assert_awaited_once_with(session)
        assert panel.frame is frame
        assert panel.start_button.disabled
        assert panel.run_notice.visible
        assert not panel.save_button.disabled
        await panel.save(None)
        assert picker.save_file.await_args.kwargs["src_bytes"] == frame.jpeg

        await panel.start(None)
        robot.start_camera.assert_awaited_once()
        panel.set_run_active(False)
        assert not panel.start_button.disabled
        assert not panel.run_notice.visible

        panel.close(update=False)
        released.assert_called_once_with(panel)
        assert not panel.content.visible

    asyncio.run(check())
