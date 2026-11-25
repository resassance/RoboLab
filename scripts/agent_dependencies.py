import subprocess
import sys
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from zipfile import ZIP_DEFLATED, ZipFile

TARGETS = {
    "aarch64": ("3.11", "3.13"),
    "armv7l": ("3.11", "3.13"),
    "x86_64": ("3.11", "3.12", "3.13"),
}
REQUIREMENTS = (
    "annotated-doc==0.0.7",
    "annotated-types==0.8.0",
    "anyio==4.16.0",
    "click==8.5.1",
    "fastapi==0.165.0",
    "h11==0.16.0",
    "idna==3.20",
    "opentelemetry-api==1.45.2",
    "pydantic==2.14.4",
    "pydantic-core==2.50.5",
    "starlette==1.10.0",
    "typing-extensions==4.16.0",
    "typing-inspection==0.4.5",
    "uvicorn==0.54.0",
    "websockets==16.1.1",
)


def bundle_agent_dependencies(destination: Path) -> None:
    with TemporaryDirectory(prefix="robolab-agent-dependencies-") as directory:
        with ZipFile(destination, "x") as bundle:
            for architecture, versions in TARGETS.items():
                for version in versions:
                    abi = "cp" + version.replace(".", "")
                    target = Path(directory) / f"{abi}-{architecture}"
                    subprocess.run(
                        [
                            sys.executable,
                            "-m",
                            "pip",
                            "install",
                            "--no-input",
                            "--disable-pip-version-check",
                            "--only-binary=:all:",
                            "--no-deps",
                            "--no-compile",
                            "--platform",
                            f"manylinux2014_{architecture}",
                            "--platform",
                            f"manylinux_2_17_{architecture}",
                            "--implementation",
                            "cp",
                            "--python-version",
                            version,
                            "--abi",
                            abi,
                            "--target",
                            str(target),
                            *REQUIREMENTS,
                        ],
                        check=True,
                    )
                    payload = BytesIO()
                    with ZipFile(payload, "w", ZIP_DEFLATED) as archive:
                        for file in sorted(target.rglob("*")):
                            relative = file.relative_to(target)
                            if file.is_file() and relative.parts[0] not in {"bin", "Scripts"}:
                                archive.write(file, relative.as_posix())
                    bundle.writestr(f"{target.name}.zip", payload.getvalue())
