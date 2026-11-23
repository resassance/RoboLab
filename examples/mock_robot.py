import argparse
import json
import signal
import sys
from threading import Event
from types import FrameType


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Симуляция робота RoboLab.", allow_abbrev=False)
    parser.add_argument(
        "--steps", type=int, default=80, help="Число шагов от 1 до 120, по умолчанию 80."
    )
    parser.add_argument(
        "--interval", type=float, default=0.5, help="Пауза между шагами от 0.1 до 2.0 секунды."
    )
    parser.add_argument("--scenario", choices=("patrol", "scan"), default="patrol")
    parser.add_argument(
        "--scan-points", type=int, help="Число точек в сценарии scan, по умолчанию 3."
    )
    parser.add_argument("--label", default="RoboLab", help="Название запуска до 80 символов.")
    parser.add_argument("--verbose", action="store_true", help="Показать диагностику запуска.")
    args = parser.parse_args()

    if not 1 <= args.steps <= 120:
        parser.error("--steps должен быть от 1 до 120")
    if not 0.1 <= args.interval <= 2.0:
        parser.error("--interval должен быть от 0.1 до 2.0")
    if not args.label.strip() or len(args.label) > 80 or not args.label.isprintable():
        parser.error("--label должен содержать от 1 до 80 печатных символов")
    if args.scan_points is None:
        args.scan_points = 3
    elif args.scenario != "scan":
        parser.error("--scan-points доступен только при --scenario scan")
    elif not 1 <= args.scan_points <= 8:
        parser.error("--scan-points должен быть от 1 до 8")
    return args


def emit(
    event: str, message: str, level: str = "INFO", *, metrics: dict[str, float] | None = None
) -> None:
    data = {"event": event, "level": level, "message": message}
    if metrics:
        data["metrics"] = metrics
    print(json.dumps(data, ensure_ascii=False), flush=True)


def main() -> None:
    args = parse_args()
    stopping = Event()

    def stop(signum: int, frame: FrameType | None) -> None:
        stopping.set()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    emit("mock_started", f"Симуляция запущена. Название: {args.label}. Сценарий: {args.scenario}.")
    print("Mock Robot: диагностический вывод stderr.", file=sys.stderr)
    if args.verbose:
        emit(
            "mock_debug", f"Число шагов: {args.steps}. Интервал: {args.interval} с.", level="DEBUG"
        )
    scan_suffix = f" Точек сканирования: {args.scan_points}." if args.scenario == "scan" else ""
    for step in range(1, args.steps + 1):
        if stopping.wait(args.interval):
            emit("mock_stopped", "Симуляция остановлена.")
            return
        emit(
            "mock_tick",
            f"Шаг симуляции {step}.{scan_suffix}",
            metrics={"step": step, "progress_percent": 100 * step / args.steps},
        )
    emit("mock_complete", "Симуляция завершена.")


if __name__ == "__main__":
    main()
