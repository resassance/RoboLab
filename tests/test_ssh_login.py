import asyncio
from unittest.mock import AsyncMock

import pytest

from client.connection import RobotError
from client.ssh import SshTarget
from client.ssh_login import SshLogin, SshProfiles, safe_poweroff
from common.protocol import SafetyState


class Preferences:
    def __init__(self):
        self.values = {}

    async def get(self, key):
        return self.values.get(key)

    async def set(self, key, value):
        self.values[key] = value
        return True

    async def remove(self, key):
        self.values.pop(key, None)
        return True


def test_ssh_profile_initially_hides_the_key_reset_button():
    login = SshLogin(None)
    login.target_changed(None)
    assert not login.forget_key_button.visible
    assert not login.remember.value


def test_ssh_password_is_stored_only_with_opt_in():
    async def check():
        profiles = SshProfiles()
        profiles.preferences = Preferences()
        profiles.secrets = Preferences()
        target = SshTarget("robot.local", 22, "pi")
        await profiles.save(target, None)
        assert await profiles.load() == (target, None)
        assert profiles.secrets.values == {}
        await profiles.save(target, "test secret")
        assert "test secret" not in repr(profiles.preferences.values)
        assert await profiles.load() == (target, "test secret")
        await profiles.save(target, None)
        assert await profiles.load() == (target, None)
        assert profiles.secrets.values == {}

    asyncio.run(check())


def test_ssh_host_key_pin_is_shared_between_users():
    async def check():
        profiles = SshProfiles()
        profiles.preferences = Preferences()
        target = SshTarget("robot.local", 22, "pi")
        await profiles.trust(target, "SHA256:test")
        assert await profiles.fingerprint(SshTarget("robot.local", 22, "operator")) == "SHA256:test"
        assert await profiles.fingerprint(SshTarget("robot.local", 2222, "pi")) is None

    asyncio.run(check())


def test_old_host_key_can_be_forgotten_without_erasing_current_profile():
    async def check():
        profiles = SshProfiles()
        profiles.preferences = Preferences()
        profiles.secrets = Preferences()
        old = SshTarget("old-robot.local", 22, "pi")
        current = SshTarget("next-robot.local", 22, "pi")
        await profiles.trust(old, "SHA256:old")
        await profiles.save(current, None)
        await profiles.forget_key(old)
        assert await profiles.fingerprint(old) is None
        assert await profiles.load() == (current, None)

    asyncio.run(check())


def test_profile_write_failure_leaves_no_orphaned_password():
    async def check():
        profiles = SshProfiles()
        profiles.preferences = Preferences()
        profiles.preferences.set = AsyncMock(side_effect=RuntimeError("storage failure"))
        profiles.secrets = Preferences()
        with pytest.raises(RuntimeError):
            await profiles.save(SshTarget("robot.local", 22, "pi"), "test secret")
        assert profiles.secrets.values == {}

    asyncio.run(check())


@pytest.mark.parametrize("cutoff", [False, None, True])
def test_shutdown_requires_confirmed_motor_cutoff(cutoff):
    agent = AsyncMock()
    agent.prepare_poweroff.return_value = SafetyState(latched=True, motor_disable_confirmed=cutoff)
    ssh = AsyncMock()
    if cutoff is True:
        asyncio.run(safe_poweroff(agent, ssh, "sudo fixture"))
        ssh.poweroff.assert_awaited_once_with("sudo fixture")
    else:
        with pytest.raises(RobotError, match="отключение приводов"):
            asyncio.run(safe_poweroff(agent, ssh, "sudo fixture"))
        ssh.poweroff.assert_not_called()


def test_shutdown_is_not_sent_if_the_operator_disconnected():
    agent = AsyncMock()
    agent.prepare_poweroff.return_value = SafetyState(latched=True, motor_disable_confirmed=True)
    ssh = AsyncMock()
    asyncio.run(safe_poweroff(agent, ssh, "", active=lambda: False))
    ssh.poweroff.assert_not_called()
