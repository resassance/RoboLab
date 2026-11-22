import json
import signal
import sys
from threading import Event
from types import FrameType

MAX_TICKS = 80


def emit(event: str, message: str) -> None:
    print(json.dumps({"event": event, "level": "INFO", "message": message}, ensure_ascii=False))


def main() -> None:
    stopping = Event()

    def stop(signum: int, frame: FrameType | None) -> None:
        stopping.set()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    emit("mock_started", "Симуляция запущена.")
    print("Mock Robot: диагностический вывод stderr.", file=sys.stderr)
    for step in range(1, MAX_TICKS + 1):
        if stopping.wait(0.5):
            emit("mock_stopped", "Симуляция остановлена.")
            return
        emit("mock_tick", f"Шаг симуляции {step}.")
    emit("mock_complete", "Симуляция завершена.")


if __name__ == "__main__":
    main()
