import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import flet as ft
import pytest

from client.files import FilePanel, open_file
from common.protocol import FileStorage


def make_panel(platform, result):
    picker = SimpleNamespace(pick_files=AsyncMock(return_value=result))
    page = SimpleNamespace(web=False, platform=platform)
    panel = FilePanel(page, None, picker, None, compact=False)
    panel.storage = FileStorage(roots=["Стенд"], max_upload_bytes=4)
    panel.root = "Стенд"
    panel.refresh = panel.render
    return panel


def test_invalid_replacement_clears_selection_but_cancel_preserves_it():
    small = ft.FilePickerFile(id=1, name="previous.bin", size=1, bytes=b"x")
    large = ft.FilePickerFile(id=2, name="new.bin", size=5, bytes=b"xxxxx")
    panel = make_panel(ft.PagePlatform.LINUX, [])
    panel.file = small

    asyncio.run(panel.pick_file(None))
    assert panel.file is small
    assert not panel.send.disabled

    panel.picker.pick_files.return_value = [large]
    asyncio.run(panel.pick_file(None))
    assert panel.file is None
    assert panel.send.disabled
    assert "Лимит робота" in panel.notice.value


def test_android_picker_cache_is_released_without_touching_other_files(tmp_path, monkeypatch):
    cache = tmp_path / "cache"
    monkeypatch.setenv("FLET_APP_STORAGE_CACHE", str(cache))

    def pick_file(directory, payload):
        directory.mkdir(parents=True)
        path = directory / "data.bin"
        path.write_bytes(payload)
        return ft.FilePickerFile(id=1, name=path.name, size=len(payload), path=str(path))

    old = pick_file(cache / "file_picker" / str(uuid4()), b"a")
    large = pick_file(cache / "file_picker" / str(uuid4()), b"large")
    accepted = pick_file(cache / "file_picker" / str(uuid4()), b"b")
    external = pick_file(tmp_path / "storage" / str(uuid4()), b"c")
    panel = make_panel(ft.PagePlatform.ANDROID, [large])
    panel.file = old

    asyncio.run(panel.pick_file(None))
    assert not Path(old.path).parent.exists()
    assert not Path(large.path).parent.exists()

    panel.picker.pick_files.return_value = [accepted]
    asyncio.run(panel.pick_file(None))
    assert panel.file is accepted
    assert Path(accepted.path).parent.exists()

    panel.picker.pick_files.return_value = [external]
    asyncio.run(panel.pick_file(None))
    assert not Path(accepted.path).parent.exists()
    panel.close()
    assert Path(external.path).read_bytes() == b"c"


def test_dropped_file_read_rejects_same_size_replacement(tmp_path):
    file = tmp_path / "report.csv"
    file.write_bytes(b"original")
    info = file.stat()
    replacement = tmp_path / "new.csv"
    replacement.write_bytes(b"replaced")
    replacement.replace(file)

    with pytest.raises(OSError):
        open_file(str(file), info.st_size, (info.st_dev, info.st_ino))
