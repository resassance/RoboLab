import shutil
import subprocess
import tomllib

from scripts.build_client import ROOT, stage_client


def test_release_staging_includes_only_tracked_client_code(tmp_path):
    repository = tmp_path / "checkout"
    repository.mkdir()
    shutil.copyfile(ROOT / "pyproject.toml", repository / "pyproject.toml")
    files = {
        "client/main.py": "async def main(page): pass\n",
        "client/notes.txt": "Private notes\n",
        "common/protocol.py": "",
        "agent/main.py": "",
        "examples/mock_robot.py": "",
    }
    for name, content in files.items():
        path = repository / name
        path.parent.mkdir(exist_ok=True)
        path.write_text(content, encoding="utf-8")
    subprocess.run(["git", "init", "--quiet"], cwd=repository, check=True)
    subprocess.run(["git", "add", "--", *files], cwd=repository, check=True)
    (repository / "client" / "untracked.py").write_text("TOKEN = 'private'\n", encoding="utf-8")
    destination = tmp_path / "package"

    version = stage_client(repository, destination)

    assert {
        p.relative_to(destination).as_posix() for p in destination.rglob("*") if p.is_file()
    } == {
        "client/main.py",
        "common/protocol.py",
        "main.py",
        "pyproject.toml",
    }
    assert "from client.main import main" in (destination / "main.py").read_text(encoding="utf-8")
    with (destination / "pyproject.toml").open("rb") as stream:
        project = tomllib.load(stream)
    assert project["project"]["version"] == version
    dependencies = project["project"]["dependencies"]
    assert all(
        any(dependency.startswith(f"{name}==") for dependency in dependencies)
        for name in ("flet", "httpx", "msgpack", "pydantic", "websockets")
    )
    assert project["tool"]["flet"]["android"]["permission"] == {"android.permission.INTERNET": True}
