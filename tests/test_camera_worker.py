import struct
import subprocess
import sys
import time
from pathlib import Path

import pytest

WORKER = Path(__file__).resolve().parents[1] / "agent" / "camera_worker.py"
MAX_JPEG_BYTES = 768 * 1024


def worker_command(device="mock", view="original", fps="8", quality="75"):
    return [
        sys.executable,
        "-I",
        "-u",
        str(WORKER),
        "--device",
        device,
        "--view",
        view,
        "--fps",
        fps,
        "--quality",
        quality,
    ]


@pytest.mark.parametrize(
    "options",
    [{"device": "/tmp/video0"}, {"view": "raw"}, {"fps": "9"}, {"quality": "34"}],
)
def test_invalid_command_writes_diagnostics_without_frame_bytes(options):
    result = subprocess.run(worker_command(**options), capture_output=True, timeout=5)
    assert result.returncode == 2
    assert result.stdout == b""
    assert b"error:" in result.stderr


def read_packets(data):
    frames = []
    while len(data) >= 4:
        (size,) = struct.unpack_from("!I", data)
        assert 0 < size <= MAX_JPEG_BYTES
        if len(data) < size + 4:
            break
        frames.append(data[4 : size + 4])
        data = data[size + 4 :]
    return frames


@pytest.mark.skipif(sys.platform != "linux", reason="The camera worker receives Linux signals")
@pytest.mark.parametrize("view", ["original", "gray", "mask", "overlay"])
def test_mock_stream_emits_decodable_jpegs_and_stops_on_sigterm(tmp_path, view):
    cv2 = pytest.importorskip("cv2")
    np = pytest.importorskip("numpy")
    output = tmp_path / "frames"
    with (
        output.open("wb") as stream,
        subprocess.Popen(
            worker_command(view=view), stdout=stream, stderr=subprocess.PIPE
        ) as worker,
    ):
        try:
            deadline = time.monotonic() + 8
            packets = []
            while len(packets) < 2 and worker.poll() is None and time.monotonic() < deadline:
                packets = read_packets(output.read_bytes())
                time.sleep(0.01)
        finally:
            if worker.poll() is None:
                worker.terminate()
            try:
                _, stderr = worker.communicate(timeout=3)
            except subprocess.TimeoutExpired:
                worker.kill()
                worker.communicate()
                raise

    assert worker.returncode == 0, stderr.decode(errors="replace")
    assert stderr == b""
    assert len(packets) >= 2
    for packet in packets[:2]:
        assert packet.startswith(b"\xff\xd8") and packet.endswith(b"\xff\xd9")
        frame = cv2.imdecode(np.frombuffer(packet, np.uint8), cv2.IMREAD_UNCHANGED)
        channels = (3,) if view in {"original", "overlay"} else ()
        assert frame.shape == (480, 640, *channels)
        assert frame.max() > frame.min()
        if view == "mask":
            assert (frame < 32).mean() > 0.8
            assert frame.max() > 224
    assert packets[0] != packets[1]


def test_v4l2_capture_resizes_frames_and_releases_on_stop(monkeypatch, capsysbinary):
    from types import SimpleNamespace

    from agent import camera_worker

    cv2 = pytest.importorskip("cv2")
    np = pytest.importorskip("numpy")

    class Capture:
        released = False
        read_count = 0

        def isOpened(self):
            return True

        def read(self):
            self.read_count += 1
            if self.read_count == 2:
                camera_worker.request_stop(None, None)
            return True, np.full((720, 1280, 3), (30, 150, 220), dtype=np.uint8)

        def release(self):
            self.released = True

    capture = Capture()

    def open_capture(device, backend):
        assert (device, backend) == ("/dev/video2", cv2.CAP_V4L2)
        return capture

    monkeypatch.setattr(cv2, "VideoCapture", open_capture)
    args = SimpleNamespace(device="/dev/video2", view="original", fps=8, quality=90)
    with pytest.raises(camera_worker.StopRequested):
        camera_worker.run(args)

    assert capture.released
    output = capsysbinary.readouterr()
    (jpeg,) = read_packets(output.out)
    assert output.err == b""
    frame = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_UNCHANGED)
    assert frame.shape == (360, 640, 3)
