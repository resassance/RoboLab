import asyncio
import os
import sys

import httpx
import pytest

from agent import main

TOKEN = "agent-token-file-abcdefghijklmnopqrstuvwxyz"
OLD_TOKEN = "agent-token-environment-abcdefghijklmnopqrstuvwxyz"
linux_only = pytest.mark.skipif(sys.platform != "linux", reason="Linux token file permissions")


@pytest.fixture
def launch(monkeypatch):
    apps = []
    monkeypatch.setenv("ROBOLAB_TOKEN", OLD_TOKEN)
    monkeypatch.setattr(main.uvicorn, "run", lambda app, **kwargs: apps.append(app))

    def startup(*args):
        monkeypatch.setattr(sys, "argv", ["robolab-agent", *map(str, args)])
        main.run()
        return apps[-1]

    return startup


async def health_code(app, token):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://robot"
    ) as client:
        response = await client.get("/health", headers={"Authorization": f"Bearer {token}"})
    return response.status_code


def test_environment_token_still_works(launch):
    assert asyncio.run(health_code(launch(), OLD_TOKEN)) == 200


@linux_only
@pytest.mark.parametrize("newline", [b"", b"\n", b"\r\n"])
def test_token_file_overrides_environment(newline, launch, tmp_path):
    config = tmp_path / ".config" / "robolab"
    config.mkdir(parents=True)
    path = config / "token"
    path.write_bytes(TOKEN.encode("ascii") + newline)
    path.chmod(0o600)
    app = launch("--token-file", path)

    assert asyncio.run(health_code(app, TOKEN)) == 200
    assert asyncio.run(health_code(app, OLD_TOKEN)) == 401


@linux_only
@pytest.mark.parametrize(
    "contents",
    [b"", TOKEN.encode("ascii") + b"\nsecret-value", b"x" * 257, b"x" * 1_048_576, b"\xff" * 40],
)
def test_invalid_token_file_cannot_fall_back_to_environment(contents, launch, tmp_path, capsys):
    path = tmp_path / "token"
    path.write_bytes(contents)
    path.chmod(0o600)

    with pytest.raises(SystemExit) as error:
        launch("--token-file", path)

    assert error.value.code == 2
    message = capsys.readouterr().err
    assert "URL-safe token" in message
    assert TOKEN not in message
    assert "secret-value" not in message


@linux_only
@pytest.mark.parametrize(
    "kind", ["public", "symlink", "ancestor-link", "directory", "fifo", "absent"]
)
def test_inaccessible_or_non_private_token_file_is_rejected(kind, launch, tmp_path, capsys):
    directory = tmp_path / "config"
    directory.mkdir()
    private_token = directory / "token"
    private_token.write_text(TOKEN, encoding="ascii")
    private_token.chmod(0o600)
    path = tmp_path / "token"
    if kind == "public":
        path = private_token
        path.chmod(0o640)
    elif kind == "symlink":
        path.symlink_to(private_token)
    elif kind == "ancestor-link":
        alias = tmp_path / "linked-config"
        alias.symlink_to(directory, target_is_directory=True)
        path = alias / "token"
    elif kind == "directory":
        path.mkdir()
    elif kind == "fifo":
        os.mkfifo(path, mode=0o600)

    with pytest.raises(SystemExit) as error:
        launch("--token-file", path)

    assert error.value.code == 2
    message = capsys.readouterr().err
    assert "unrecognized arguments" not in message
    assert TOKEN not in message
