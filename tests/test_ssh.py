import asyncio
import os
from contextlib import asynccontextmanager

import asyncssh
import pytest

TOKEN = "synthetic-robolab-token-abcdef0123456789"
PASSWORD = "synthetic-ssh-password"


class FakeRobot:
    def __init__(self, root):
        self.root = root
        self.key = asyncssh.generate_private_key("ssh-ed25519")
        self.passwords = []
        self.forwarding = []
        self.commands = []
        self.command_status = 0
        self.token_file = root / ".config" / "robolab" / "token"
        self.token_file.parent.mkdir(parents=True)
        self.token_file.write_bytes(TOKEN.encode("ascii") + b"\r\n")

    async def process(self, process):
        self.commands.append((process.command, await process.stdin.read(), process.term_type))
        if self.command_status is None:
            process.channel.get_connection().abort()
        else:
            process.exit(self.command_status)

    @asynccontextmanager
    async def listening(self):
        from client.ssh import SshTarget

        async def echo(reader, writer):
            try:
                writer.write(await reader.read(20))
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()

        async with await asyncio.start_server(echo, "127.0.0.1", 0) as echo_server:
            local_port = echo_server.sockets[0].getsockname()[1]
            robot = self

            class PasswordServer(asyncssh.SSHServer):
                def connection_made(self, connection):
                    self.connection = connection

                def password_auth_supported(self):
                    return True

                def validate_password(self, username, password):
                    robot.passwords.append((username, password))
                    return username == "pi" and password == PASSWORD

                def connection_requested(self, dest_host, dest_port, orig_host, orig_port):
                    robot.forwarding.append((dest_host, dest_port))
                    return self.connection.forward_connection("127.0.0.1", local_port)

            async with await asyncssh.listen(
                "127.0.0.1",
                0,
                server_factory=PasswordServer,
                server_host_keys=[self.key],
                process_factory=self.process,
                sftp_factory=lambda channel: asyncssh.SFTPServer(
                    channel, chroot=os.fsencode(self.root)
                ),
                encoding=None,
                allow_pty=False,
            ) as listener:
                yield SshTarget("127.0.0.1", listener.get_port(), "pi")


def test_pin_is_required_before_password_and_forwarding_uses_only_loopback(tmp_path):
    from client.ssh import HostKeyChanged, SshSession, UnknownHostKey

    async def check():
        robot = FakeRobot(tmp_path)
        async with robot.listening() as target:
            with pytest.raises(UnknownHostKey) as error:
                await SshSession.connect(target, PASSWORD, None)
            fingerprint = robot.key.get_fingerprint("sha256")
            assert error.value.fingerprint == fingerprint
            assert not robot.passwords

            other_key = asyncssh.generate_private_key("ssh-ed25519")
            with pytest.raises(HostKeyChanged):
                await SshSession.connect(target, PASSWORD, other_key.get_fingerprint("sha256"))
            assert not robot.passwords

            session = await SshSession.connect(target, PASSWORD, fingerprint)
            try:
                assert session.token == TOKEN
                assert not session.closed
                assert robot.passwords == [("pi", PASSWORD)]
                host, _, port = session.address.removeprefix("http://").rpartition(":")
                assert host == "127.0.0.1"
                reader, writer = await asyncio.open_connection(host, int(port))
                try:
                    writer.write(b"health")
                    await writer.drain()
                    assert await asyncio.wait_for(reader.readexactly(6), 2) == b"health"
                    assert robot.forwarding == [("127.0.0.1", 8765)]
                finally:
                    writer.close()
                    await writer.wait_closed()
            finally:
                await session.close()
                await session.close()
            assert session.closed
            with pytest.raises(OSError):
                await asyncio.open_connection(host, int(port))

    asyncio.run(check())


@pytest.mark.parametrize("payload", [b"x" * 259, TOKEN.encode() + b"\n\n", b"!" * 40])
def test_malformed_token_prevents_session(tmp_path, payload):
    from client.ssh import SshError, SshSession

    async def check():
        robot = FakeRobot(tmp_path)
        robot.token_file.write_bytes(payload)
        async with robot.listening() as target:
            with pytest.raises(SshError, match="токен") as error:
                await SshSession.connect(target, PASSWORD, robot.key.get_fingerprint("sha256"))
            assert TOKEN not in str(error.value)
        assert not robot.forwarding

    asyncio.run(check())


def test_poweroff_uses_fixed_command_and_never_requests_a_terminal(tmp_path):
    from client.ssh import PoweroffUnconfirmed, SshError, SshSession

    async def check():
        robot = FakeRobot(tmp_path)
        async with robot.listening() as target:
            session = await SshSession.connect(
                target, PASSWORD, robot.key.get_fingerprint("sha256")
            )
            try:
                password = "synthetic '$(ignored)'"
                await session.poweroff(password)
                await session.poweroff("")
                assert robot.commands == [
                    ("sudo -S -p '' -- /usr/sbin/poweroff", password.encode() + b"\n", None),
                    ("sudo -n -- /usr/sbin/poweroff", b"", None),
                ]
                with pytest.raises(SshError):
                    await session.poweroff("invalid\npassword")
                assert len(robot.commands) == 2
                robot.command_status = 1
                with pytest.raises(SshError) as error:
                    await session.poweroff(password)
                assert not isinstance(error.value, PoweroffUnconfirmed)
                assert password not in str(error.value)
                robot.command_status = None
                with pytest.raises(PoweroffUnconfirmed):
                    await session.poweroff(password)
            finally:
                await session.close()

    asyncio.run(check())


@pytest.mark.parametrize(
    ("host", "port", "username"),
    [
        ("http://pi.local", 22, "pi"),
        ("pi.local:22", 22, "pi"),
        ("pi.local", 0, "pi"),
        ("pi.local", 22, "pi user"),
    ],
)
def test_invalid_ssh_target(host, port, username):
    from client.ssh import SshTarget

    with pytest.raises(ValueError):
        SshTarget(host, port, username)
