import asyncio
import os
import shlex
import sys
import sysconfig
from contextlib import asynccontextmanager
from importlib.metadata import PackageNotFoundError, distribution
from io import BytesIO
from pathlib import Path
from venv import EnvBuilder
from zipfile import ZIP_DEFLATED, ZipFile

import asyncssh
import httpx
import pytest


@asynccontextmanager
async def loopback_robot(home, *, python=sys.executable, block_installs=False):
    class TestSSHServer(asyncssh.SSHServer):
        def begin_auth(self, username):
            return False

    async def execute(request):
        command = shlex.split(request.command)
        assert command[:5] == ["exec", "python3", "-I", "-u", "-c"]
        prelude = (
            "import os;from pathlib import Path;"
            "Path.home=classmethod(lambda cls: cls(os.environ['ROBOLAB_TEST_HOME']));"
        )
        if block_installs:
            prelude += (
                "\nimport sys\n"
                "def deny_install(event, args):\n"
                "    if event == 'subprocess.Popen' and any("
                "module in args[1] for module in ('venv', 'pip')):\n"
                "        raise RuntimeError('Installing on the robot is unavailable')\n"
                "sys.addaudithook(deny_install)\n"
            )
        child = await asyncio.create_subprocess_exec(
            python,
            *command[2:5],
            prelude + command[5],
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "ROBOLAB_TEST_HOME": os.fspath(home)},
        )

        async def send_input():
            try:
                while data := await request.stdin.read(32768):
                    child.stdin.write(data)
                    await child.stdin.drain()
            finally:
                child.stdin.close()

        relay = asyncio.create_task(send_input())
        try:
            while data := await child.stdout.read(32768):
                request.stdout.write(data)
                await request.stdout.drain()
            result = await child.wait()
            request.exit(result)
        finally:
            relay.cancel()
            await asyncio.gather(relay, return_exceptions=True)
            if child.returncode is None:
                child.terminate()
                await child.wait()

    async with await asyncssh.listen(
        "127.0.0.1",
        0,
        server_factory=TestSSHServer,
        server_host_keys=[asyncssh.generate_private_key("ssh-ed25519")],
        process_factory=execute,
        encoding=None,
    ) as server:
        async with asyncssh.connect("127.0.0.1", server.get_port(), known_hosts=None) as client:
            yield client


def make_dependency_bundle(path):
    distributions = (
        "annotated-doc",
        "annotated-types",
        "anyio",
        "click",
        "fastapi",
        "h11",
        "idna",
        "opentelemetry-api",
        "pydantic",
        "pydantic-core",
        "starlette",
        "typing-extensions",
        "typing-inspection",
        "uvicorn",
        "websockets",
    )
    buffer = BytesIO()
    with ZipFile(buffer, "w", ZIP_DEFLATED) as dependencies:
        for name in distributions:
            try:
                package = distribution(name)
            except PackageNotFoundError:
                if name in {"annotated-doc", "opentelemetry-api"}:
                    continue
                raise
            for relative in package.files or ():
                if ".." not in relative.parts and relative.suffix != ".pyc":
                    dependencies.write(package.locate_file(relative), relative.as_posix())
    architecture = sysconfig.get_config_var("MULTIARCH").split("-")[0]
    if architecture == "arm":
        architecture = "armv7l"
    tag = f"cp{sys.version_info.major}{sys.version_info.minor}-{architecture}"
    with ZipFile(path, "w") as bundle:
        bundle.writestr(f"{tag}.zip", buffer.getvalue())


@pytest.mark.skipif(sys.platform != "linux", reason="Raspberry Pi agent requires Linux")
def test_starts_agent_on_python_without_pip(tmp_path, monkeypatch):
    from client import ssh_agent

    bundle = tmp_path / "robolab-agent-dependencies.zip"
    make_dependency_bundle(bundle)
    monkeypatch.setattr(ssh_agent, "DEPENDENCY_BUNDLE", bundle, raising=False)
    interpreter = tmp_path / "clean-python"
    EnvBuilder(with_pip=False).create(interpreter)

    async def check():
        async with loopback_robot(
            tmp_path, python=interpreter / "bin" / "python", block_installs=True
        ) as connection:
            agent = await ssh_agent.start_agent(connection)
            try:
                async with httpx.AsyncClient() as client:
                    result = await client.get(
                        f"http://127.0.0.1:{agent.port}/health",
                        headers={"Authorization": f"Bearer {agent.token}"},
                    )
                    assert result.status_code == 200
                    assert result.json()["robot"] == "mock"
            finally:
                await agent.close()

    asyncio.run(check())
    assert list((tmp_path / ".robolab-ssh").iterdir()) == [
        Path(tmp_path, ".robolab-ssh", "agent.lock")
    ]


@pytest.mark.skipif(sys.platform != "linux", reason="Raspberry Pi agent requires Linux")
def test_starts_agent_in_clean_account_and_cleans_up_on_disconnect(tmp_path):
    from client.ssh_agent import AgentStartError, start_agent

    async def check():
        async with loopback_robot(tmp_path) as connection:
            agent = await start_agent(connection)
            try:
                url = f"http://127.0.0.1:{agent.port}/health"
                async with httpx.AsyncClient() as client:
                    assert (await client.get(url)).status_code == 401
                    result = await client.get(
                        url, headers={"Authorization": f"Bearer {agent.token}"}
                    )
                    assert result.status_code == 200
                    assert result.json()["robot"] == "mock"

                with pytest.raises(AgentStartError, match="другом подключении"):
                    await start_agent(connection)
            finally:
                await agent.close()

            with pytest.raises(OSError):
                await asyncio.open_connection("127.0.0.1", agent.port)
            assert not (tmp_path / ".config" / "robolab" / "token").exists()
            assert {file.name for file in (tmp_path / ".robolab-ssh").iterdir()} == {"agent.lock"}

    asyncio.run(check())


@pytest.mark.skipif(sys.platform != "linux", reason="Raspberry Pi agent requires Linux")
def test_invalid_explicit_hardware_directory_reports_error(tmp_path):
    from client.ssh_agent import AgentStartError, start_agent

    async def check():
        async with loopback_robot(tmp_path) as connection:
            with pytest.raises(AgentStartError, match="robot_navigation.py"):
                await start_agent(connection, script_dir=str(tmp_path))

    asyncio.run(check())


@pytest.mark.skipif(sys.platform != "linux", reason="Raspberry Pi agent requires Linux")
def test_agent_stops_when_ssh_connection_is_lost(tmp_path):
    from client.ssh_agent import start_agent

    async def check():
        async with loopback_robot(tmp_path) as connection:
            agent = await start_agent(connection)
            connection.abort()
            async with asyncio.timeout(10):
                while True:
                    try:
                        _, writer = await asyncio.open_connection("127.0.0.1", agent.port)
                    except OSError:
                        break
                    writer.close()
                    await writer.wait_closed()
                    await asyncio.sleep(0.05)
                while any((tmp_path / ".robolab-ssh").glob("session-*")):
                    await asyncio.sleep(0.05)

    asyncio.run(check())
