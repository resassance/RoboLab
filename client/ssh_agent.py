import asyncio
import base64
import json
import re
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from zipfile import ZIP_DEFLATED, BadZipFile, ZipFile

import asyncssh

from common.protocol import valid_token

REMOTE_PROGRAM = "client/ssh_agent_remote.py"
BUNDLE = "robolab-agent.zip"
DEPENDENCY_BUNDLE = Path(__file__).with_name("robolab-agent-dependencies.zip")
MAX_ARCHIVE = 1_048_576
MAX_DEPENDENCY_ARCHIVE = 8_388_608

AGENT_FILES = {
    REMOTE_PROGRAM,
    "agent/__init__.py",
    "agent/camera.py",
    "agent/camera_worker.py",
    "agent/experiments.py",
    "agent/files.py",
    "agent/main.py",
    "agent/manual_drive.py",
    "agent/motor_off.py",
    "agent/supervisor.py",
    "agent/telemetry.py",
    "agent/yahboom.py",
    "common/__init__.py",
    "common/file_paths.py",
    "common/parameters.py",
    "common/protocol.py",
    "examples/__init__.py",
    "examples/mock_robot.json",
    "examples/mock_robot.py",
}

ERRORS = {
    "platform": "Автозапуск агента требует Linux с Python 3.11 или новее.",
    "private_directory": "Нет доступа к закрытой папке ~/.robolab-ssh на роботе.",
    "another_agent": "Автоматический агент уже работает в другом подключении.",
    "dependencies": (
        "Не удалось загрузить зависимости для Python на Pi. "
        "Проверьте совместимость версии Python со сборкой RoboLab."
    ),
    "script_directory": (
        "В указанной папке должны быть читаемые robot_navigation.py и robot_config.json."
    ),
    "gpio": "У пользователя SSH нет доступа к GPIO или модулю RPi.GPIO.",
    "hardware_config": "Проверьте настройки Yahboom и доступ пользователя SSH к их путям.",
    "startup": "Не удалось запустить агент RoboLab в домашней папке на Pi.",
}


class AgentStartError(Exception):
    pass


def agent_archive() -> bytes:
    packaged = Path(__file__).with_name(BUNDLE)
    if packaged.is_file():
        return packaged.read_bytes()
    buffer = BytesIO()
    root = packaged.parent.parent
    with ZipFile(buffer, "w", ZIP_DEFLATED) as archive:
        for name in sorted(AGENT_FILES):
            path = root / name
            if path.is_symlink() or not path.is_file():
                raise OSError(f"Invalid agent source: {name}")
            archive.write(path, name)
    return buffer.getvalue()


def dependency_archive(tag: str) -> bytes:
    if not re.fullmatch(r"cp[0-9]+-(?:aarch64|armv7l|x86_64)", tag):
        raise ValueError("Invalid dependency target")
    try:
        with ZipFile(DEPENDENCY_BUNDLE) as bundle, bundle.open(f"{tag}.zip") as archive:
            payload = archive.read(MAX_DEPENDENCY_ARCHIVE + 1)
    except (FileNotFoundError, KeyError):
        return b""
    except (OSError, BadZipFile) as exc:
        raise AgentStartError("Не удалось прочитать комплект зависимостей агента.") from exc
    if len(payload) > MAX_DEPENDENCY_ARCHIVE:
        raise AgentStartError("Комплект зависимостей агента слишком большой.")
    return payload


async def read_response(process: asyncssh.SSHClientProcess[bytes]) -> dict[str, object]:
    size = int.from_bytes(await process.stdout.readexactly(4), "big")
    if not 1 <= size <= 1024:
        raise ValueError("Invalid agent response size")
    result = json.loads(await process.stdout.readexactly(size))
    if not isinstance(result, dict):
        raise ValueError("Invalid agent response")
    error = result.get("error")
    if isinstance(error, str):
        raise AgentStartError(ERRORS.get(error, ERRORS["startup"]))
    return result


@dataclass
class StartedAgent:
    port: int
    token: str
    _process: asyncssh.SSHClientProcess[bytes]

    async def close(self) -> None:
        await close_process(self._process)


async def close_process(process: asyncssh.SSHClientProcess[bytes]) -> None:
    try:
        process.stdin.write_eof()
        async with asyncio.timeout(12):
            await process.wait_closed()
    except (asyncssh.Error, OSError):
        pass
    finally:
        process.close()


async def start_agent(
    connection: asyncssh.SSHClientConnection, *, script_dir: str | None = None
) -> StartedAgent:
    if script_dir is not None and (
        not script_dir.startswith("/")
        or len(script_dir.encode("utf-8")) > 4096
        or any(character in script_dir for character in "\0\r\n")
    ):
        raise AgentStartError("Укажите абсолютный путь к папке Yahboom на Pi.")
    try:
        bundle = agent_archive()
        if len(bundle) > MAX_ARCHIVE:
            raise ValueError("Agent bundle exceeds the size limit")
        with ZipFile(BytesIO(bundle)) as archive:
            loader = base64.b64encode(archive.read(REMOTE_PROGRAM)).decode("ascii")
    except (OSError, ValueError, KeyError, BadZipFile) as exc:
        raise AgentStartError("В приложении отсутствует комплект файлов агента.") from exc

    command = f"""exec python3 -I -u -c 'import base64;exec(base64.b64decode("{loader}"))'"""
    header = json.dumps({"size": len(bundle), "script_dir": script_dir}, ensure_ascii=False)
    process = None
    try:
        async with asyncio.timeout(240):
            process = await connection.create_process(
                command, encoding=None, stderr=asyncssh.DEVNULL, request_pty=False
            )
            process.stdin.write(header.encode() + b"\n")
            process.stdin.write(bundle)
            await process.stdin.drain()
            result = await read_response(process)
            target = result.get("dependencies")
            if isinstance(target, str):
                payload = dependency_archive(target)
                process.stdin.write(len(payload).to_bytes(4, "big"))
                process.stdin.write(payload)
                await process.stdin.drain()
                result = await read_response(process)
            port, token = result.get("port"), result.get("token")
            if type(port) is not int or not 1 <= port <= 65535:
                raise ValueError("Invalid agent port")
            if not isinstance(token, str) or not valid_token(token):
                raise ValueError("Invalid agent token")
            agent = StartedAgent(port, token, process)
            process = None
            return agent
    except TimeoutError as exc:
        raise AgentStartError(
            "Pi не успел подготовить агент. Проверьте подключение по SSH и повторите."
        ) from exc
    except (asyncssh.Error, OSError, ValueError, EOFError) as exc:
        raise AgentStartError(
            "Не удалось запустить агент через SSH. Проверьте, что на Pi доступен python3."
        ) from exc
    finally:
        if process is not None:
            await close_process(process)
