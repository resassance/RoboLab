import asyncio
import os
from contextlib import asynccontextmanager
from types import SimpleNamespace

import asyncssh
import flet as ft
import pytest

from client.connection import RobotError
from client.files import FilePanel
from client.sftp_files import SftpFiles


class TestSshServer(asyncssh.SSHServer):
    __test__ = False

    def begin_auth(self, username):
        return False


@asynccontextmanager
async def sftp_files(home):
    async with (
        await asyncssh.listen(
            "127.0.0.1",
            0,
            server_factory=TestSshServer,
            server_host_keys=[asyncssh.generate_private_key("ssh-ed25519")],
            sftp_factory=lambda channel: asyncssh.SFTPServer(channel, chroot=os.fsencode(home)),
        ) as server,
        asyncssh.connect("127.0.0.1", server.get_port(), username="pi", known_hosts=None) as ssh,
    ):
        yield SftpFiles(ssh)


async def chunks(*items):
    for item in items:
        yield item


def test_lists_folders_and_saves_into_selected_folder(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    folder = home / "Запуски и логи"
    folder.mkdir()
    (home / "readme").touch()
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (home / "Ложная папка").symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        if getattr(exc, "winerror", None) != 1314:
            raise
    (home / ".robolab-upload-reserved").mkdir()

    async def check():
        async with sftp_files(home) as files:
            storage = await files.file_storage()
            assert storage.max_upload_bytes == 32 * 1024**2
            (root,) = storage.roots
            assert await files.file_folders(root, ()) == [folder.name]
            assert await files.file_folders(root, (folder.name,)) == []
            for path in [("..",), ("Ложная папка",), ("bad/name",)]:
                with pytest.raises(RobotError):
                    await files.file_folders(root, path)
            with pytest.raises(RobotError):
                await files.upload_file(root, ("Ложная папка",), "x.txt", chunks(b"x"), 1)
            with pytest.raises(RobotError):
                await files.upload_file(root, (), "../x.txt", chunks(b"x"), 1)
            receipt = await files.upload_file(
                root, (folder.name,), "замеры.txt", chunks(b"ab", b"", b"cd"), 4
            )
            assert receipt.model_dump() == {
                "root": root,
                "path": [folder.name],
                "name": "замеры.txt",
                "size": 4,
            }
            await files.upload_file(root, (), "empty", chunks(), 0)

    asyncio.run(check())
    assert (folder / "замеры.txt").read_bytes() == b"abcd"
    assert (home / "empty").stat().st_size == 0
    assert not list(outside.iterdir())
    assert not list(folder.glob(".robolab-upload-*"))


def test_upload_never_overwrites_and_cleans_staging_on_failure_or_cancellation(tmp_path):
    async def check():
        async with sftp_files(tmp_path) as files:
            storage = await files.file_storage()
            (root,) = storage.roots
            existing = tmp_path / "report"
            existing.write_bytes(b"previous")
            with pytest.raises(RobotError, match="уже существует"):
                await files.upload_file(root, (), "report", chunks(b"new"), 3)
            assert existing.read_bytes() == b"previous"

            with pytest.raises(RobotError):
                await files.upload_file(root, (), "partial", chunks(b"partial"), 10)
            assert not (tmp_path / "partial").exists()
            with pytest.raises(RobotError):
                await files.upload_file(
                    root, (), "over", chunks(b"1234"), storage.max_upload_bytes + 1
                )
            assert not (tmp_path / "over").exists()

            staged = asyncio.Event()

            async def waiting_source():
                yield b"ab"
                staged.set()
                await asyncio.Future()

            upload = asyncio.create_task(
                files.upload_file(root, (), "cancelled", waiting_source(), 4)
            )
            await asyncio.wait_for(staged.wait(), 3)
            assert list(tmp_path.glob(".robolab-upload-*"))
            upload.cancel()
            with pytest.raises(asyncio.CancelledError):
                await upload
            assert not (tmp_path / "cancelled").exists()
            assert not list(tmp_path.glob(".robolab-upload-*"))

    asyncio.run(check())


def test_upload_handles_competing_writers_without_replacing_destination(tmp_path):
    async def check():
        async with sftp_files(tmp_path) as files:
            (root,) = (await files.file_storage()).roots
            arrived = asyncio.Barrier(2)

            async def concurrent_source(content):
                await arrived.wait()
                yield content

            result = await asyncio.wait_for(
                asyncio.gather(
                    files.upload_file(root, (), "same", concurrent_source(b"a"), 1),
                    files.upload_file(root, (), "same", concurrent_source(b"b"), 1),
                    return_exceptions=True,
                ),
                3,
            )
            failures = [entry for entry in result if isinstance(entry, RobotError)]
            assert len(failures) == 1 and "уже существует" in str(failures[0])
            assert (tmp_path / "same").read_bytes() in (b"a", b"b")
            assert not list(tmp_path.glob(".robolab-upload-*"))

    asyncio.run(check())


def test_create_folder_inside_selected_sftp_path(tmp_path):
    parent = tmp_path / "parent"
    parent.mkdir()
    (parent / "taken").write_bytes(b"old")

    async def check():
        async with sftp_files(tmp_path) as files:
            (root,) = (await files.file_storage()).roots
            await files.create_folder(root, ("parent",), "Новая папка")
            assert await files.file_folders(root, ("parent",)) == ["Новая папка"]
            with pytest.raises(RobotError, match="уже есть"):
                await files.create_folder(root, ("parent",), "taken")
            with pytest.raises(RobotError):
                await files.create_folder(root, ("parent",), "..")

    asyncio.run(check())
    assert (parent / "taken").read_bytes() == b"old"


def test_dropped_files_and_folders_are_sent_to_open_directory(tmp_path, monkeypatch):
    home = tmp_path / "remote"
    target = home / "experiment"
    target.mkdir(parents=True)
    (target / "taken.txt").write_bytes(b"existing")
    folder = tmp_path / "Запуск"
    (folder / "empty").mkdir(parents=True)
    (folder / "trace.csv").write_bytes(b"1,2")
    outside = tmp_path / "taken.txt"
    outside.write_bytes(b"new")
    other = tmp_path / "readme.txt"
    other.write_bytes(b"notes")

    async def check():
        async with sftp_files(home) as files:
            page = SimpleNamespace(web=False, platform=ft.PagePlatform.LINUX)
            panel = FilePanel(page, files, None, None, compact=False)
            panel.refresh = panel.render
            monkeypatch.setattr(panel.progress_row, "update", lambda: None)
            await panel.load_storage()
            await panel.load_folders(panel.root, ("experiment",))
            await panel.drop_paths([str(folder), str(outside), str(other)])
            await panel.task
            assert panel.folders == ["Запуск"]
            assert "taken.txt" in panel.notice.value

            (folder / "trace.csv").write_bytes(b"new")
            (folder / "z_retry.txt").write_bytes(b"retry")
            await panel.drop_paths([str(folder)])
            await panel.task
            assert "trace.csv" in panel.notice.value

    asyncio.run(check())
    assert (target / "Запуск" / "empty").is_dir()
    assert (target / "Запуск" / "z_retry.txt").read_bytes() == b"retry"
    assert (target / "Запуск" / "trace.csv").read_bytes() == b"1,2"
    assert (target / "readme.txt").read_bytes() == b"notes"
    assert (target / "taken.txt").read_bytes() == b"existing"
