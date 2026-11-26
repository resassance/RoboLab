import argparse
import json
import shutil
import subprocess
import sys
import tomllib
from hashlib import file_digest
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.request import urlopen
from zipfile import ZIP_DEFLATED, ZipFile

ROOT = Path(__file__).resolve().parents[1]
VOICE_MODEL = "vosk-model-small-ru-0.22"
VOICE_MODEL_URL = f"https://alphacephei.com/vosk/models/{VOICE_MODEL}.zip"
VOICE_MODEL_SHA256 = "961d5ff98a17f4aa6de69864d0aa71fa5bac682301d2b5d17a3f24c5c99a46d4"
VOICE_ARCHIVE_LIMIT = 50_000_000
LAUNCHER = """import flet as ft

from client.main import main

if __name__ == "__main__":
    ft.run(main)
"""


def download_voice_model(target: Path) -> None:
    with urlopen(VOICE_MODEL_URL, timeout=60) as response, target.open("xb") as output:
        total = 0
        while chunk := response.read(1024 * 1024):
            total += len(chunk)
            if total > VOICE_ARCHIVE_LIMIT:
                raise ValueError("Vosk model archive is too large")
            output.write(chunk)


def stage_voice_model(root: Path, destination: Path, archive: Path) -> None:
    with archive.open("rb") as source:
        if file_digest(source, "sha256").hexdigest() != VOICE_MODEL_SHA256:
            raise ValueError("Vosk model SHA-256 mismatch")
        source.seek(0)
        with ZipFile(source) as model:
            for item in model.infolist():
                if item.is_dir():
                    continue
                relative = Path(item.filename).relative_to(VOICE_MODEL)
                path = destination / "client" / "voice_model" / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                with model.open(item) as origin, path.open("xb") as target:
                    shutil.copyfileobj(origin, target)
    shutil.copyfile(
        root / "extensions" / "voice" / "model-LICENSE",
        destination / "client" / "voice_model" / "LICENSE",
    )


def extension_wheel(work: Path, name: str, pattern: str) -> Path:
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            "--no-deps",
            "--wheel-dir",
            str(work),
            str(ROOT / "extensions" / name),
        ],
        check=True,
    )
    (wheel,) = work.glob(pattern)
    return wheel


def stage_client(
    root: Path,
    destination: Path,
    *,
    platform: str = "linux",
    dropzone_wheel: Path | None = None,
    voice_wheel: Path | None = None,
    voice_archive: Path | None = None,
) -> str:
    with (root / "pyproject.toml").open("rb") as stream:
        project = tomllib.load(stream)["project"]
    version = project["version"]
    python_requirement = ">=3.14,<3.15" if platform == "apk" else ">=3.12,<3.13"
    dependencies = [
        dependency.replace("flet[cli]", "flet")
        for dependency in project["optional-dependencies"]["release"]
    ]
    if dropzone_wheel is not None and platform in {"linux", "windows"}:
        dependencies.append(f"robolab-dropzone @ {dropzone_wheel.resolve().as_uri()}")
    if voice_archive is not None and platform in {"linux", "windows"}:
        dependencies.extend(project["optional-dependencies"]["voice-desktop"])
    if voice_wheel is not None and platform == "apk":
        dependencies.append(f"robolab-voice-android @ {voice_wheel.resolve().as_uri()}")
    android_audio = '"android.permission.RECORD_AUDIO" = true\n' if voice_wheel else ""
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

    agent_paths = subprocess.check_output(
        [
            "git",
            "ls-files",
            "-z",
            "--",
            "agent/*.py",
            "common/*.py",
            "examples/__init__.py",
            "examples/mock_robot.py",
            "examples/mock_robot.json",
            "client/ssh_agent_remote.py",
        ],
        cwd=root,
    ).decode("utf-8")
    with ZipFile(destination / "client" / "robolab-agent.zip", "x", ZIP_DEFLATED) as archive:
        for name in filter(None, agent_paths.split("\0")):
            path = root / name
            if path.is_symlink():
                raise ValueError(f"Cannot package a symbolic link: {name}")
            archive.write(path, name)

    if voice_archive is not None:
        stage_voice_model(root, destination, voice_archive)
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
{android_audio}""",
        encoding="utf-8",
    )
    return version


def build_client(
    platform: str, output: Path, build_number: int, voice_archive: Path | None = None
) -> Path:
    output.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="robolab-build-", dir=output) as temp:
        work = Path(temp)
        app = work / "app"
        bundle = work / "bundle"
        if voice_archive is None:
            voice_archive = work / f"{VOICE_MODEL}.zip"
            download_voice_model(voice_archive)
        dropzone = voice = None
        if platform in {"linux", "windows"}:
            dropzone = extension_wheel(work, "dropzone", "robolab_dropzone-*.whl")
        else:
            voice = extension_wheel(work, "voice", "robolab_voice_android-*.whl")
        version = stage_client(
            ROOT,
            app,
            platform=platform,
            dropzone_wheel=dropzone,
            voice_wheel=voice,
            voice_archive=voice_archive,
        )
        if __package__:
            from .agent_dependencies import bundle_agent_dependencies
        else:
            from agent_dependencies import bundle_agent_dependencies

        bundle_agent_dependencies(app / "client" / "robolab-agent-dependencies.zip")
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
    parser.add_argument("--voice-model-archive", type=Path)
    args = parser.parse_args()
    if not 1 <= args.build_number <= 2_100_000_000:
        parser.error("--build-number must be between 1 and 2100000000")
    print(
        build_client(
            args.platform, args.output.resolve(), args.build_number, args.voice_model_archive
        )
    )


if __name__ == "__main__":
    main()
