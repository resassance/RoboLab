import argparse
import json
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = """import flet as ft

from client.main import main

if __name__ == "__main__":
    ft.run(main)
"""


def stage_client(root: Path, destination: Path, *, platform: str = "linux") -> str:
    with (root / "pyproject.toml").open("rb") as stream:
        project = tomllib.load(stream)["project"]
    version = project["version"]
    python_requirement = ">=3.14,<3.15" if platform == "apk" else ">=3.12,<3.13"
    dependencies = [
        dependency.replace("flet[cli]", "flet")
        for dependency in project["optional-dependencies"]["release"]
    ]
    paths = subprocess.check_output(
        ["git", "ls-files", "-z", "--", "client/*.py", "common/*.py"], cwd=root
    ).decode("utf-8")
    destination.mkdir()
    for name in filter(None, paths.split("\0")):
        path = Path(name)
        source = root / path
        if source.is_symlink():
            raise ValueError(f"Cannot package a symbolic link: {path}")
        target = destination / path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)

    (destination / "main.py").write_text(LAUNCHER, encoding="utf-8")
    (destination / "pyproject.toml").write_text(
        f"""[project]
name = "robolab"
version = "{version}"
requires-python = "{python_requirement}"
dependencies = {json.dumps(dependencies)}

[tool.flet]
product = "RoboLab"
org = "io.github.resassance"

[tool.flet.android]
min_sdk_version = 24

[tool.flet.android.permission]
"android.permission.INTERNET" = true
""",
        encoding="utf-8",
    )
    return version


def build_client(platform: str, output: Path, build_number: int) -> Path:
    output.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="robolab-build-", dir=output) as temp:
        work = Path(temp)
        app = work / "app"
        bundle = work / "bundle"
        version = stage_client(ROOT, app, platform=platform)
        command = [
            sys.executable,
            "-m",
            "flet_cli.cli",
            "build",
            platform,
            str(app),
            "--output",
            str(bundle),
            "--artifact",
            "robolab",
            "--build-number",
            str(build_number),
            "--yes",
        ]
        if platform == "apk":
            command += ["--arch", "arm64-v8a", "x86_64"]
        subprocess.run(command, check=True)

        artifact_base = work / f"RoboLab-{version}-{platform}-x64"
        if platform == "apk":
            candidates = list(bundle.rglob("*.apk"))
            if len(candidates) != 1:
                raise RuntimeError(f"Expected one universal APK, found {len(candidates)}")
            artifact = candidates[0].replace(work / f"RoboLab-{version}-android.apk")
        else:
            executable = "robolab.exe" if platform == "windows" else "robolab"
            if not (bundle / executable).is_file():
                raise RuntimeError(f"Missing {executable} in Flet build output")
            kind = "zip" if platform == "windows" else "gztar"
            artifact = Path(shutil.make_archive(str(artifact_base), kind, root_dir=bundle))

        return artifact.replace(output / artifact.name)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("platform", choices=("linux", "windows", "apk"))
    parser.add_argument("--output", type=Path, default=ROOT / "dist")
    parser.add_argument("--build-number", type=int, default=1)
    args = parser.parse_args()
    if not 1 <= args.build_number <= 2_100_000_000:
        parser.error("--build-number must be between 1 and 2100000000")
    print(build_client(args.platform, args.output.resolve(), args.build_number))


if __name__ == "__main__":
    main()
