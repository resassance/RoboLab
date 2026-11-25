import shutil
import subprocess
import tomllib
from zipfile import ZipFile

import pytest

from scripts.build_client import ROOT, stage_client


@pytest.mark.parametrize(
    ("platform", "python_requirement"),
    [("linux", ">=3.12,<3.13"), ("windows", ">=3.12,<3.13"), ("apk", ">=3.14,<3.15")],
)
def test_release_staging_includes_only_tracked_sources(tmp_path, platform, python_requirement):
    repository = tmp_path / "checkout"
    repository.mkdir()
    shutil.copyfile(ROOT / "pyproject.toml", repository / "pyproject.toml")
    files = {
        "client/main.py": "async def main(page): pass\n",
        "client/ssh_agent_remote.py": "pass\n",
        "client/notes.txt": "Private notes\n",
        "common/protocol.py": "",
        "agent/main.py": "",
        "examples/mock_robot.py": "",
        "examples/mock_robot.json": "{}\r\n",
    }
    for name, content in files.items():
        path = repository / name
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(content.encode("utf-8"))
    subprocess.run(["git", "init", "--quiet"], cwd=repository, check=True)
    subprocess.run(["git", "add", "--", *files], cwd=repository, check=True)
    (repository / "client" / "untracked.py").write_text("TOKEN = 'private'\n", encoding="utf-8")
    (repository / "agent" / "untracked.py").write_text("private\n", encoding="utf-8")
    destination = tmp_path / "package"

    version = stage_client(repository, destination, platform=platform)

    assert {
        p.relative_to(destination).as_posix() for p in destination.rglob("*") if p.is_file()
    } == {
        "client/main.py",
        "client/robolab-agent.zip",
        "client/ssh_agent_remote.py",
        "common/protocol.py",
        "main.py",
        "pyproject.toml",
    }
    with ZipFile(destination / "client" / "robolab-agent.zip") as bundle:
        assert set(bundle.namelist()) == {
            "client/ssh_agent_remote.py",
            "common/protocol.py",
            "agent/main.py",
            "examples/mock_robot.py",
            "examples/mock_robot.json",
        }
        assert (
            bundle.read("examples/mock_robot.json")
            == (repository / "examples/mock_robot.json").read_bytes()
        )
    assert "from client.main import main" in (destination / "main.py").read_text(encoding="utf-8")
    with (destination / "pyproject.toml").open("rb") as stream:
        project = tomllib.load(stream)
    assert project["project"]["version"] == version
    assert project["project"]["requires-python"] == python_requirement
    dependencies = project["project"]["dependencies"]
    assert all(
        any(dependency.startswith(f"{name}==") for dependency in dependencies)
        for name in (
            "asyncssh",
            "flet",
            "flet-secure-storage",
            "httpx",
            "msgpack",
            "pydantic",
            "websockets",
        )
    )
    assert project["tool"]["flet"]["android"]["permission"] == {"android.permission.INTERNET": True}
