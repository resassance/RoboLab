import fcntl
import json
import os
import secrets
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
from io import BytesIO
from pathlib import Path, PurePosixPath
from time import monotonic
from zipfile import ZipFile

DEPENDENCIES = (
    "fastapi>=0.115,<1",
    "uvicorn>=0.35,<1",
    "websockets>=15,<17",
    "pydantic>=2.10,<3",
)
CHECK_DEPENDENCIES = """
from importlib.metadata import version
for package, lower, upper in (
    ("fastapi", (0, 115), (1, 0)),
    ("uvicorn", (0, 35), (1, 0)),
    ("pydantic", (2, 10), (3, 0)),
    ("websockets", (15, 0), (17, 0)),
):
    current = tuple(map(int, version(package).split(".")[:2]))
    if not lower <= current < upper:
        raise SystemExit(1)
from uvicorn.protocols.websockets.websockets_sansio_impl import WebSocketsSansIOProtocol
"""


class StartupError(Exception):
    pass


def report(**result):
    response = json.dumps(result, separators=(",", ":")).encode("ascii")
    sys.stdout.buffer.write(len(response).to_bytes(4, "big") + response)
    sys.stdout.buffer.flush()


def unpack(archive, destination):
    with ZipFile(BytesIO(archive)) as source:
        if sum(entry.file_size for entry in source.infolist()) > 2_097_152:
            raise StartupError("startup")
        for entry in source.infolist():
            if entry.filename == "client/ssh_agent_remote.py":
                continue
            path = PurePosixPath(entry.filename)
            if (
                len(path.parts) != 2
                or path.parts[0] not in {"agent", "common", "examples"}
                or (path.suffix != ".py" and path.as_posix() != "examples/mock_robot.json")
            ):
                raise StartupError("startup")
            target = destination.joinpath(*path.parts)
            target.parent.mkdir(exist_ok=True)
            with target.open("xb") as output:
                output.write(source.read(entry))


def run_check(command, disconnected, timeout):
    if disconnected.is_set():
        raise StartupError("startup")
    with subprocess.Popen(
        command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    ) as process:
        deadline = monotonic() + timeout
        while process.poll() is None:
            if disconnected.wait(0.1) or monotonic() >= deadline:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                return False
        return process.returncode == 0


def dependency_path(runtime, disconnected):
    if run_check([sys.executable, "-I", "-c", CHECK_DEPENDENCIES], disconnected, 10):
        return None
    env = runtime / f"python-{sys.version_info.major}.{sys.version_info.minor}"
    python = env / "bin" / "python"
    if not python.is_file() and not run_check(
        [sys.executable, "-I", "-m", "venv", str(env)], disconnected, 30
    ):
        raise StartupError("venv")
    if not run_check([str(python), "-I", "-c", CHECK_DEPENDENCIES], disconnected, 10):
        if not run_check(
            [
                str(python),
                "-I",
                "-m",
                "pip",
                "--isolated",
                "install",
                "--no-input",
                "--disable-pip-version-check",
                "--only-binary=:all:",
                *DEPENDENCIES,
            ],
            disconnected,
            175,
        ):
            raise StartupError("dependencies")
    python_version = f"python{sys.version_info.major}.{sys.version_info.minor}"
    return env / "lib" / python_version / "site-packages"


def config_path(home, runtime, workspace, script_dir, disconnected):
    for config in (
        home / ".config" / "robolab" / "yahboom.local.json",
        Path("/etc/robolab/yahboom.local.json"),
    ):
        if config.exists():
            return config
    if script_dir is None:
        return None
    path = Path(script_dir)
    files = [path / name for name in ("robot_navigation.py", "robot_config.json")]
    if not path.is_absolute() or any(
        not file.is_file() or not os.access(file, os.R_OK) for file in files
    ):
        raise StartupError("script_directory")
    python = Path("/usr/bin/python3")
    if not python.is_file():
        python = Path(sys.executable)
    device = Path("/dev/gpiomem")
    if not os.access(device, os.R_OK | os.W_OK) or not run_check(
        [str(python), "-I", "-c", "import RPi.GPIO"], disconnected, 10
    ):
        raise StartupError("gpio")

    log_dir = runtime / "yahboom-logs"
    log_dir.mkdir(exist_ok=True)
    system_lock = Path("/run/robolab/yahboom.lock")
    lock_file = system_lock if system_lock.exists() else runtime / "yahboom.lock"
    config = workspace / "yahboom.local.json"
    config.write_text(
        json.dumps(
            {
                "python": str(python),
                "script_dir": str(path),
                "config_file": str(files[1]),
                "log_dir": str(log_dir),
                "lock_file": str(lock_file),
                "allow_motion": False,
            }
        ),
        encoding="utf-8",
    )
    return config


def serve(workspace, home, config, packages, disconnected):
    sys.path.insert(0, str(workspace))
    if packages is not None:
        sys.path.insert(1, str(packages))
    import uvicorn

    from agent.main import create_app, load_yahboom

    try:
        hardware = load_yahboom(config) if config is not None else None
    except (ValueError, OSError) as exc:
        raise StartupError("hardware_config") from exc
    token = secrets.token_urlsafe(32)
    app = create_app(token, hardware, data_dir=home / ".robolab" / "data")

    class SshServer(uvicorn.Server):
        async def startup(self, sockets=None):
            if disconnected.is_set():
                self.should_exit = True
                return
            await super().startup(sockets)
            if self.started:
                report(port=listener.getsockname()[1], token=token)

        async def on_tick(self, counter):
            return disconnected.is_set() or await super().on_tick(counter)

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        server = SshServer(
            uvicorn.Config(
                app, ws="websockets-sansio", log_level="warning", timeout_graceful_shutdown=5
            )
        )
        server.run(sockets=[listener])


def start():
    if sys.platform != "linux" or sys.version_info < (3, 11):
        raise StartupError("platform")

    header = json.loads(sys.stdin.buffer.readline(8192))
    size = header["size"]
    if type(size) is not int or not 1 <= size <= 1_048_576:
        raise StartupError("startup")
    archive = sys.stdin.buffer.read(size)
    if len(archive) != size:
        raise StartupError("startup")

    disconnected = threading.Event()

    def watch_input():
        try:
            os.read(sys.stdin.fileno(), 1)
        finally:
            disconnected.set()

    threading.Thread(target=watch_input, daemon=True).start()
    signal.signal(signal.SIGHUP, lambda *_: disconnected.set())
    signal.signal(signal.SIGTERM, lambda *_: disconnected.set())

    os.umask(0o077)
    home = Path.home().resolve(strict=True)
    runtime = home / ".robolab-ssh"
    try:
        runtime.mkdir(mode=0o700, exist_ok=True)
        info = runtime.lstat()
    except OSError as exc:
        raise StartupError("private_directory") from exc
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise StartupError("private_directory")

    fd = os.open(runtime / "agent.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb"):
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise StartupError("private_directory")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise StartupError("another_agent") from exc
        with tempfile.TemporaryDirectory(prefix="session-", dir=runtime) as directory:
            workspace = Path(directory)
            unpack(archive, workspace)
            config = config_path(home, runtime, workspace, header["script_dir"], disconnected)
            packages = dependency_path(runtime, disconnected)
            serve(workspace, home, config, packages, disconnected)


if __name__ == "__main__":
    try:
        start()
    except StartupError as error:
        report(error=str(error))
    except Exception:
        report(error="startup")
