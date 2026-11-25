import asyncio
import ipaddress
import re
from dataclasses import dataclass
from typing import Self

import asyncssh

from client.sftp_files import SftpFiles
from client.ssh_agent import AgentStartError, StartedAgent, start_agent
from common.protocol import valid_token

LOOPBACK = "127.0.0.1"
AGENT_PORT = 8765
TOKEN_PATH = ".config/robolab/token"
TOKEN_FILE_LIMIT = 258


class SshError(Exception):
    pass


class UnknownHostKey(SshError):
    def __init__(self, fingerprint: str) -> None:
        super().__init__("Подтвердите отпечаток ключа SSH.")
        self.fingerprint = fingerprint


class HostKeyChanged(SshError):
    pass


class PoweroffUnconfirmed(SshError):
    pass


@dataclass(frozen=True, slots=True)
class SshTarget:
    host: str
    port: int
    username: str

    def __post_init__(self) -> None:
        host = self.host.strip()
        bracketed = host.startswith("[") and host.endswith("]")
        if bracketed:
            host = host[1:-1]
        if not re.fullmatch(r"[A-Za-z0-9._:%-]{1,254}", host):
            raise ValueError("Введите имя хоста или IP-адрес робота.")
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            if bracketed or not all(
                re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
                for label in host.removesuffix(".").split(".")
            ):
                raise ValueError("Введите имя хоста или IP-адрес робота.") from None
            host = host.lower()
        else:
            if bracketed and address.version != 6:
                raise ValueError("Квадратные скобки допустимы только для IPv6.")
            host = address.compressed

        if type(self.port) is not int or not 1 <= self.port <= 65535:
            raise ValueError("Порт SSH должен быть числом от 1 до 65535.")
        username = self.username.strip()
        if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,62}\$?", username):
            raise ValueError("Проверьте имя пользователя SSH.")
        object.__setattr__(self, "host", host)
        object.__setattr__(self, "username", username)


class _PinnedClient(asyncssh.SSHClient):
    def __init__(self, password: str, expected: str | None) -> None:
        self.password: str | None = password
        self.expected = expected
        self.fingerprint: str | None = None

    def validate_host_public_key(
        self, host: str, addr: str, port: int, key: asyncssh.SSHKey
    ) -> bool:
        self.fingerprint = key.get_fingerprint("sha256")
        return self.fingerprint == self.expected

    def password_auth_requested(self) -> str | None:
        password, self.password = self.password, None
        return password


async def _read_token(connection: asyncssh.SSHClientConnection) -> str:
    try:
        async with asyncio.timeout(5), connection.start_sftp_client() as sftp:
            attrs = await sftp.lstat(TOKEN_PATH)
            if attrs.type != asyncssh.FILEXFER_TYPE_REGULAR:
                raise SshError("Файл токена RoboLab должен быть обычным файлом.")
            if attrs.size is not None and attrs.size > TOKEN_FILE_LIMIT:
                raise SshError("Файл токена RoboLab превышает допустимый размер.")
            data = bytearray()
            async with sftp.open(TOKEN_PATH, "rb") as token_file:
                while len(data) <= TOKEN_FILE_LIMIT:
                    chunk = await token_file.read(TOKEN_FILE_LIMIT + 1 - len(data))
                    if not chunk:
                        break
                    data.extend(chunk)
    except (asyncssh.SFTPNoSuchFile, asyncssh.SFTPNoSuchPath) as exc:
        raise SshError(f"SSH-вход выполнен. На роботе нет файла ~/{TOKEN_PATH}.") from exc
    except asyncssh.SFTPPermissionDenied as exc:
        raise SshError(f"SSH-вход выполнен. Проверьте права на ~/{TOKEN_PATH} на роботе.") from exc
    except asyncssh.ChannelOpenError as exc:
        raise SshError("SSH-вход выполнен. Сервер робота не открыл канал SFTP.") from exc
    except (asyncssh.Error, OSError) as exc:
        raise SshError(f"SSH-вход выполнен. Не удалось прочитать ~/{TOKEN_PATH} по SFTP.") from exc

    if len(data) > TOKEN_FILE_LIMIT:
        raise SshError("Файл токена RoboLab превышает допустимый размер.")
    try:
        token = data.decode("ascii")
    except UnicodeDecodeError:
        raise SshError("Файл токена RoboLab содержит недопустимые символы.") from None
    token = token.removesuffix("\r\n") if token.endswith("\r\n") else token.removesuffix("\n")
    if not valid_token(token):
        raise SshError("Файл токена RoboLab содержит недопустимый токен.")
    return token


class SshSession:
    def __init__(self, connection: asyncssh.SSHClientConnection) -> None:
        self._connection = connection
        self._listener: asyncssh.SSHListener | None = None
        self._agent: StartedAgent | None = None

    @property
    def files(self) -> SftpFiles:
        return SftpFiles(self._connection)

    @property
    def closed(self) -> bool:
        return self._connection.is_closed()

    @property
    def manages_agent(self) -> bool:
        return self._agent is not None

    @classmethod
    async def connect(cls, target: SshTarget, password: str, fingerprint: str | None) -> Self:
        if not password:
            raise SshError("Введите пароль SSH.")
        if fingerprint is not None and not re.fullmatch(r"SHA256:[A-Za-z0-9+/]{43}", fingerprint):
            raise HostKeyChanged("Сохранённый отпечаток ключа SSH недействителен.")
        verifier = _PinnedClient(password, fingerprint)
        try:
            connection = await asyncssh.connect(
                target.host,
                port=target.port,
                username=target.username,
                config=None,
                known_hosts=((), (), ()),
                server_host_key_algs="default",
                x509_trusted_certs=None,
                client_factory=lambda: verifier,
                client_keys=None,
                agent_path=None,
                public_key_auth=False,
                host_based_auth=False,
                kbdint_auth=False,
                gss_auth=False,
                gss_kex=False,
                password_auth=True,
                preferred_auth="password",
                disable_trivial_auth=True,
                connect_timeout=10,
                keepalive_interval=10,
                keepalive_count_max=2,
            )
        except asyncssh.HostKeyNotVerifiable as exc:
            if fingerprint is not None:
                raise HostKeyChanged("Ключ SSH робота отличается от сохранённого.") from exc
            if verifier.fingerprint is not None:
                raise UnknownHostKey(verifier.fingerprint) from exc
            raise SshError("Не удалось проверить ключ SSH робота.") from exc
        except asyncssh.PermissionDenied as exc:
            raise SshError("Робот отклонил имя пользователя или пароль SSH.") from exc
        except (asyncssh.Error, OSError, TimeoutError) as exc:
            raise SshError("Не удалось подключиться к роботу по SSH.") from exc
        finally:
            verifier.password = None
        return cls(connection)

    async def wait_closed(self) -> None:
        await self._connection.wait_closed()

    async def use_existing_agent(self) -> tuple[str, str]:
        token = await _read_token(self._connection)
        return await self._open_tunnel(AGENT_PORT), token

    async def prepare_agent(self, script_dir: str | None) -> tuple[str, str]:
        if await self._has_existing_service():
            return await self.use_existing_agent()
        try:
            agent = await start_agent(self._connection, script_dir=script_dir)
        except AgentStartError as exc:
            raise SshError(str(exc)) from exc
        try:
            address = await self._open_tunnel(agent.port)
        except BaseException:
            await agent.close()
            raise
        self._agent = agent
        return address, agent.token

    async def _has_existing_service(self) -> bool:
        try:
            async with asyncio.timeout(5):
                _, writer = await self._connection.open_connection(LOOPBACK, AGENT_PORT)
                writer.close()
                await writer.wait_closed()
        except asyncssh.ChannelOpenError as exc:
            if exc.code == asyncssh.OPEN_CONNECT_FAILED:
                return False
            raise SshError("Сервер робота отклонил проброс порта SSH.") from exc
        except (asyncssh.Error, OSError) as exc:
            raise SshError("Не удалось проверить подключение к агенту через SSH.") from exc
        return True

    async def _open_tunnel(self, port: int) -> str:
        try:
            async with asyncio.timeout(5):
                listener = await self._connection.forward_local_port(LOOPBACK, 0, LOOPBACK, port)
        except (asyncssh.Error, OSError) as exc:
            raise SshError("Не удалось открыть локальный туннель SSH.") from exc
        self._listener = listener
        return f"http://{LOOPBACK}:{listener.get_port()}"

    async def close_agent(self) -> None:
        listener, self._listener = self._listener, None
        agent, self._agent = self._agent, None
        if listener is not None:
            listener.close()
        if agent is not None:
            await agent.close()

    async def close(self) -> None:
        try:
            await self.close_agent()
        finally:
            self._connection.close()
        try:
            async with asyncio.timeout(2):
                await self._connection.wait_closed()
        except TimeoutError:
            self._connection.abort()
        except asyncio.CancelledError:
            self._connection.abort()
            raise

    async def poweroff(self, password: str) -> None:
        if self.closed:
            raise SshError("SSH-соединение закрыто.")
        if any(character in password for character in "\0\r\n"):
            raise SshError("Пароль sudo содержит недопустимые символы.")
        command = (
            "sudo -S -p '' -- /usr/sbin/poweroff" if password else "sudo -n -- /usr/sbin/poweroff"
        )
        try:
            async with (
                asyncio.timeout(15),
                self._connection.create_process(
                    command,
                    input=password + "\n" if password else None,
                    stdin=asyncssh.DEVNULL,
                    stdout=asyncssh.DEVNULL,
                    stderr=asyncssh.DEVNULL,
                    request_pty=False,
                ) as process,
            ):
                result = await process.wait()
        except asyncssh.ChannelOpenError as exc:
            raise SshError("Робот не открыл SSH-канал для выключения.") from exc
        except (asyncssh.Error, OSError, TimeoutError) as exc:
            raise PoweroffUnconfirmed("Робот не подтвердил выключение.") from exc
        if result.returncode is None:
            raise PoweroffUnconfirmed("Робот не подтвердил выключение.")
        if result.returncode != 0:
            raise SshError("Робот отклонил команду выключения. Проверьте пароль и права sudo.")
