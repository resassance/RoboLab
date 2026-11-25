import importlib
import subprocess
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile


def test_packages_individual_raspberry_pi_and_linux_dependency_archives(tmp_path, monkeypatch):
    def install_binary_wheels(command, *, check):
        target = Path(command[command.index("--target") + 1])
        abi = command[command.index("--abi") + 1]
        platform = command[command.index("--platform") + 1]
        target.mkdir()
        (target / "native.so").write_text(f"{abi} {platform}", encoding="ascii")
        (target / "bin").mkdir()
        (target / "bin" / "uvicorn").write_bytes(b"unused console entry point")

    monkeypatch.setattr(subprocess, "run", install_binary_wheels)
    dependencies = importlib.import_module("scripts.agent_dependencies")
    destination = tmp_path / "robolab-agent-dependencies.zip"
    dependencies.bundle_agent_dependencies(destination)

    with ZipFile(destination) as bundle:
        assert set(bundle.namelist()) == {
            "cp311-aarch64.zip",
            "cp311-armv7l.zip",
            "cp311-x86_64.zip",
            "cp312-x86_64.zip",
            "cp313-aarch64.zip",
            "cp313-armv7l.zip",
            "cp313-x86_64.zip",
        }
        with ZipFile(BytesIO(bundle.read("cp311-armv7l.zip"))) as archive:
            assert archive.namelist() == ["native.so"]
            assert archive.read("native.so") == b"cp311 manylinux2014_armv7l"
