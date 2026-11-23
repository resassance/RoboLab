import asyncio
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from agent.main import create_app

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux upload storage")

TOKEN = "local-files-test-token-with-at-least-32-characters"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
UPLOAD = {**AUTH, "Content-Type": "application/octet-stream"}


def test_upload_is_opt_in_and_requires_token(tmp_path):
    with TestClient(create_app(TOKEN)) as api:
        assert api.get("/health", headers=AUTH).json().get("files_available", False) is False
        assert api.get("/files", headers=AUTH).status_code == 404

    with TestClient(create_app(TOKEN, file_roots={"Данные": tmp_path})) as api:
        assert api.get("/files").status_code == 401
        assert api.get("/files/folders", params={"root": "Данные"}).status_code == 401
        assert (
            api.post(
                "/files/upload", params={"root": "Данные", "name": "measurements.csv"}
            ).status_code
            == 401
        )
        assert api.get("/health", headers=AUTH).json()["files_available"] is True
        assert api.get("/files", headers=AUTH).json() == {
            "roots": ["Данные"],
            "max_upload_bytes": 32 * 1024 * 1024,
        }


def test_nested_folder_list_and_binary_upload(tmp_path):
    nested = tmp_path / "Сеанс" / "Измерения"
    nested.mkdir(parents=True)
    (nested / "notes.txt").write_text("log")
    (nested / "Новые").mkdir()
    (tmp_path / "outside").symlink_to(nested, target_is_directory=True)
    query = [("root", "Данные"), ("path", "Сеанс"), ("path", "Измерения")]
    with TestClient(create_app(TOKEN, file_roots={"Данные": tmp_path})) as api:
        assert api.get("/files/folders", params={"root": "Данные"}, headers=AUTH).json() == [
            "Сеанс"
        ]
        assert api.get("/files/folders", params=query, headers=AUTH).json() == ["Новые"]
        reply = api.post(
            "/files/upload",
            params=[*query, ("name", "Опыт.bin")],
            content=b"\x00\xff\x10",
            headers=UPLOAD,
        )

    assert reply.status_code == 201
    assert reply.json() == {
        "root": "Данные",
        "path": ["Сеанс", "Измерения"],
        "name": "Опыт.bin",
        "size": 3,
    }
    assert (nested / "Опыт.bin").read_bytes() == b"\x00\xff\x10"
    assert sorted(file.name for file in nested.iterdir()) == ["notes.txt", "Новые", "Опыт.bin"]


def test_traversal_and_symlink_do_not_write_outside_root(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "shortcut").symlink_to(tmp_path, target_is_directory=True)
    with TestClient(create_app(TOKEN, file_roots={"data": root})) as api:
        for params, status in (
            ({"root": "unknown"}, 404),
            ({"root": "data", "path": ".."}, 400),
            ({"root": "data", "path": "/tmp"}, 400),
            ({"root": "data", "path": "a/b"}, 400),
            ({"root": "data", "path": "shortcut"}, 404),
        ):
            assert api.get("/files/folders", headers=AUTH, params=params).status_code == status
            assert (
                api.post(
                    "/files/upload",
                    headers=UPLOAD,
                    params={**params, "name": "bad.bin"},
                    content=b"x",
                ).status_code
                == status
            )
        for name in ("../bad.bin", "bad\x00.bin", "a\\bad.bin", "bad\n.bin"):
            assert (
                api.post(
                    "/files/upload",
                    headers=UPLOAD,
                    params={"root": "data", "name": name},
                    content=b"x",
                ).status_code
                == 400
            )
    assert {file.name for file in tmp_path.iterdir()} == {"root"}
    assert {file.name for file in root.iterdir()} == {"shortcut"}


def test_concurrent_upload_never_replaces_a_file(tmp_path):
    with TestClient(create_app(TOKEN, file_roots={"data": tmp_path})) as api:

        def send(data):
            reply = api.post(
                "/files/upload",
                params={"root": "data", "name": "same.bin"},
                content=data,
                headers=UPLOAD,
            )
            return reply.status_code, data

        with ThreadPoolExecutor(max_workers=2) as workers:
            results = dict(workers.map(send, (b"first", b"second")))

    assert set(results) == {201, 409}
    assert (tmp_path / "same.bin").read_bytes() == results[201]
    assert [file.name for file in tmp_path.iterdir()] == ["same.bin"]


def test_size_limit_and_incomplete_body_leave_no_files(tmp_path):
    query = {"root": "data", "name": "data.bin"}
    with TestClient(create_app(TOKEN, file_roots={"data": tmp_path}, max_upload_bytes=4)) as api:
        assert (
            api.post("/files/upload", params=query, content=b"long!", headers=UPLOAD).status_code
            == 413
        )
        assert (
            api.post(
                "/files/upload", params=query, content=iter((b"lon", b"g!")), headers=UPLOAD
            ).status_code
            == 413
        )
        assert (
            api.post(
                "/files/upload",
                params=query,
                content=b"x",
                headers={**UPLOAD, "Content-Length": "4"},
            ).status_code
            == 400
        )
    assert list(tmp_path.iterdir()) == []


def test_disconnect_discards_a_partially_written_file(tmp_path):
    app = create_app(TOKEN, file_roots={"data": tmp_path})
    messages = []
    received = 0

    async def exercise():
        async def receive():
            nonlocal received
            received += 1
            if received == 1:
                return {"type": "http.request", "body": b"partial", "more_body": True}
            assert not (tmp_path / "data.bin").exists()
            return {"type": "http.disconnect"}

        async def send(message):
            messages.append(message)

        await app(
            {
                "type": "http",
                "method": "POST",
                "path": "/files/upload",
                "query_string": b"root=data&name=data.bin",
                "headers": [
                    (name.lower().encode(), value.encode()) for name, value in UPLOAD.items()
                ],
            },
            receive,
            send,
        )

    asyncio.run(exercise())
    assert received == 2
    assert messages[0]["status"] == 400
    assert list(tmp_path.iterdir()) == []
