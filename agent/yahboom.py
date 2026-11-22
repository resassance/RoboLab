import asyncio
import os
import stat
import sys
from pathlib import Path

from pydantic import BaseModel, ConfigDict, StrictBool, field_validator

from common.parameters import Parameter
from common.protocol import Script

MOTOR_OFF_TIMEOUT = 2.0
MOTOR_OFF_SCRIPT = Path(__file__).resolve().with_name("motor_off.py")


class DeviceBusy(Exception):
    pass


class YahboomSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    python: Path
    script_dir: Path
    config_file: Path
    log_dir: Path
    lock_file: Path
    allow_motion: StrictBool = False

    @field_validator("python", "script_dir", "config_file", "log_dir", "lock_file")
    @classmethod
    def absolute_path(cls, value: Path) -> Path:
        if not value.is_absolute() or "\x00" in str(value):
            raise ValueError("Укажите абсолютный путь.")
        return value


def _duration(default: float, maximum: float) -> Parameter:
    return Parameter(
        key="max_seconds",
        flag="--max-seconds",
        kind="float",
        title="Лимит времени, с",
        description="Максимальная длительность сценария.",
        group="Запуск",
        default=default,
        minimum=1,
        maximum=maximum,
    )


class Yahboom:
    def __init__(self, settings: YahboomSettings) -> None:
        self.settings = settings
        self.script_path = _validate_paths(settings)
        distance = Script(
            id="yahboom-distance",
            name="Дальномер Yahboom",
            description="Измерение расстояния ультразвуком без движения колёс.",
            requires_heartbeat=True,
            parameters=[
                Parameter(
                    key="cycles",
                    flag="--cycles",
                    kind="int",
                    title="Количество измерений",
                    description="Число циклов измерения расстояния.",
                    group="Запуск",
                    default=1,
                    minimum=1,
                    maximum=20,
                ),
                _duration(12.0, 30.0),
            ],
        )
        self.catalog = {distance.id: distance}
        if settings.allow_motion:
            auto = Script(
                id="yahboom-auto",
                name="Объезд препятствий Yahboom",
                description="Движение с ультразвуковым объездом препятствий.",
                requires_heartbeat=True,
                parameters=[_duration(10.0, 20.0)],
            )
            self.catalog[auto.id] = auto

    def arguments(self, script_id: str, validated_args: list[str]) -> list[str]:
        if script_id not in self.catalog:
            raise KeyError(script_id)
        mode = ["--mode=distance"]
        if script_id == "yahboom-auto":
            mode = ["--mode=auto", "--obstacle-mode=bypass"]
        return [
            f"--config={self.settings.config_file}",
            f"--log-dir={self.settings.log_dir}",
            *mode,
            *validated_args,
        ]

    def acquire(self) -> int:
        import fcntl

        flags = os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
        fd = os.open(self.settings.lock_file, flags, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise OSError("Блокировка Yahboom требует обычный файл.")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise DeviceBusy from exc
        except BaseException:
            os.close(fd)
            raise
        return fd

    async def motor_off(self, lock_fd: int) -> None:
        environment = os.environ.copy()
        environment.pop("ROBOLAB_TOKEN", None)
        process = await asyncio.create_subprocess_exec(
            self.settings.python,
            "-I",
            MOTOR_OFF_SCRIPT,
            pass_fds=(lock_fd,),
            env=environment,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            status = await asyncio.wait_for(process.wait(), timeout=MOTOR_OFF_TIMEOUT)
        except (asyncio.CancelledError, TimeoutError) as exc:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.wait()
            if isinstance(exc, TimeoutError):
                raise OSError("Истекло время отключения моторов Yahboom.") from exc
            raise
        if status:
            raise OSError(f"Отключение моторов Yahboom завершилось с кодом {status}.")


def _validate_paths(settings: YahboomSettings) -> Path:
    if sys.platform != "linux":
        raise ValueError("Адаптер Yahboom доступен только в Linux.")
    script_dir = settings.script_dir.resolve(strict=True)
    script_path = script_dir / "robot_navigation.py"
    if not settings.python.is_file() or not os.access(settings.python, os.X_OK):
        raise ValueError("Интерпретатор Yahboom должен быть исполняемым файлом.")
    if script_path.is_symlink() or not script_path.is_file() or not os.access(script_path, os.R_OK):
        raise ValueError("В каталоге Yahboom требуется читаемый файл robot_navigation.py.")
    if not settings.config_file.is_file() or not os.access(settings.config_file, os.R_OK):
        raise ValueError("Конфигурация робота должна быть читаемым файлом.")
    if not settings.log_dir.is_dir() or not os.access(settings.log_dir, os.W_OK | os.X_OK):
        raise ValueError("Укажите существующий каталог журналов с доступом на запись.")
    lock_path = settings.lock_file
    if not lock_path.parent.is_dir() or not os.access(lock_path.parent, os.X_OK):
        raise ValueError("Укажите существующий каталог для файла блокировки.")
    try:
        lock_stat = lock_path.lstat()
    except FileNotFoundError:
        if not os.access(lock_path.parent, os.W_OK):
            raise ValueError("Каталог файла блокировки должен быть доступен на запись.") from None
    else:
        if not stat.S_ISREG(lock_stat.st_mode) or not os.access(lock_path, os.R_OK | os.W_OK):
            raise ValueError("Файл блокировки должен быть обычным читаемым и записываемым файлом.")
    return script_path


def load_yahboom(path: Path) -> Yahboom:
    return Yahboom(YahboomSettings.model_validate_json(path.read_bytes()))
