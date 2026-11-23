import argparse
import math
import re
import signal
import struct
import sys
import time

WIDTH = 640
HEIGHT = 480
MAX_JPEG_BYTES = 768 * 1024


class StopRequested(Exception):
    pass


def request_stop(signum, frame):
    raise StopRequested


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--device", required=True)
    parser.add_argument("--view", required=True, choices=("original", "gray", "mask", "overlay"))
    parser.add_argument("--fps", type=int, required=True)
    parser.add_argument("--quality", type=int, required=True)
    args = parser.parse_args()
    if args.device != "mock" and not re.fullmatch(r"/dev/video[0-9]+", args.device):
        parser.error("--device must be mock or /dev/videoN")
    if not 1 <= args.fps <= 8:
        parser.error("--fps must be between 1 and 8")
    if not 35 <= args.quality <= 90:
        parser.error("--quality must be between 35 and 90")
    return args


def mock_background(cv2):
    import numpy as np

    frame = np.full((HEIGHT, WIDTH, 3), (30, 26, 22), dtype=np.uint8)
    for x in range(32, WIDTH, 32):
        cv2.line(frame, (x, 80), (x, 448), (44, 40, 36), 1)
    for y in range(96, HEIGHT - 31, 32):
        cv2.line(frame, (32, y), (608, y), (44, 40, 36), 1)

    font = cv2.FONT_HERSHEY_SIMPLEX
    cv2.putText(
        frame,
        "RoboLab  /  Camera calibration",
        (32, 46),
        font,
        0.8,
        (232, 238, 239),
        2,
        cv2.LINE_AA,
    )
    cv2.rectangle(frame, (88, 126), (164, 202), (225, 154, 49), -1)
    triangle = np.array([(300, 119), (249, 202), (351, 202)], dtype=np.int32)
    cv2.fillConvexPoly(frame, triangle, (112, 207, 96), lineType=cv2.LINE_AA)
    for row in range(4):
        for column in range(6):
            x, y = 441 + column * 20, 123 + row * 20
            color = (228, 228, 228) if (row + column) % 2 == 0 else (53, 53, 53)
            cv2.rectangle(frame, (x, y), (x + 19, y + 19), color, -1)
    return frame


def mock_frame(cv2, background, elapsed):
    frame = background.copy()
    center = (320 + int(171 * math.sin(elapsed * 1.3)), 333 + int(39 * math.cos(elapsed * 1.7)))
    cv2.circle(frame, center, 57, (85, 123, 250), -1, cv2.LINE_AA)
    return frame


def fit_frame(cv2, frame):
    height, width = frame.shape[:2]
    scale = min(WIDTH / width, HEIGHT / height)
    if scale >= 1:
        return frame
    size = (max(1, round(width * scale)), max(1, round(height * scale)))
    return cv2.resize(frame, size, interpolation=cv2.INTER_AREA)


def render_view(cv2, frame, view):
    if view == "original":
        return frame
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    if view == "gray":
        return gray
    mask = cv2.Canny(cv2.GaussianBlur(gray, (5, 5), 0), 45, 95)
    if view == "mask":
        return mask

    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    contours = [contour for contour in contours if cv2.contourArea(contour) >= 24]
    if contours:
        color = (75, 245, 255)
        cv2.drawContours(frame, contours, -1, color, 2, cv2.LINE_AA)
        largest = max(contours, key=cv2.contourArea)
        moments = cv2.moments(largest)
        x = round(moments["m10"] / moments["m00"])
        y = round(moments["m01"] / moments["m00"])
        cv2.drawMarker(frame, (x, y), color, cv2.MARKER_CROSS, 21, 2, cv2.LINE_AA)
        label = f"({x}, {y}) px"
        origin = (max(0, min(x + 14, frame.shape[1] - 119)), max(17, y - 12))
        cv2.putText(
            frame, label, origin, cv2.FONT_HERSHEY_SIMPLEX, 0.48, (16, 16, 16), 4, cv2.LINE_AA
        )
        cv2.putText(frame, label, origin, cv2.FONT_HERSHEY_SIMPLEX, 0.48, color, 1, cv2.LINE_AA)
    return frame


def write_frame(output, jpeg):
    packet = memoryview(struct.pack("!I", len(jpeg)) + jpeg)
    while packet:
        written = output.write(packet)
        if not written:
            raise OSError("Could not write frame to stdout")
        packet = packet[written:]
    output.flush()


def stream_frames(cv2, args, capture, background):
    output = sys.stdout.buffer
    jpeg_options = [cv2.IMWRITE_JPEG_QUALITY, args.quality]
    interval = 1 / args.fps
    started_at = next_frame_at = time.monotonic()
    while True:
        time.sleep(max(0, next_frame_at - time.monotonic()))
        if capture is None:
            frame = mock_frame(cv2, background, time.monotonic() - started_at)
        else:
            ok, frame = capture.read()
            if not ok or frame is None or not frame.size:
                raise RuntimeError(f"Could not read a frame from {args.device}")
        view = render_view(cv2, fit_frame(cv2, frame), args.view)
        ok, encoded = cv2.imencode(".jpg", view, jpeg_options)
        if not ok or not 0 < encoded.nbytes <= MAX_JPEG_BYTES:
            raise RuntimeError("Could not encode a JPEG within the frame size limit")
        write_frame(output, encoded.tobytes())
        next_frame_at = time.monotonic() + interval


def run(args):
    import cv2

    cv2.setNumThreads(1)
    if args.device == "mock":
        stream_frames(cv2, args, None, mock_background(cv2))
        return

    capture = None
    try:
        capture = cv2.VideoCapture(args.device, cv2.CAP_V4L2)
        if not capture.isOpened():
            raise RuntimeError(f"Could not open {args.device} with V4L2")
        stream_frames(cv2, args, capture, None)
    finally:
        if capture is not None:
            capture.release()


def main():
    args = parse_args()
    try:
        signal.signal(signal.SIGTERM, request_stop)
        signal.signal(signal.SIGINT, request_stop)
        run(args)
    except (StopRequested, BrokenPipeError):
        return 0
    except Exception as error:
        print(f"Camera worker: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
