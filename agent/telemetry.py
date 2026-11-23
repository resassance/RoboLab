import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic


@dataclass
class SystemSample:
    timestamp: datetime
    cpu_ticks: tuple[int, int] | None = None
    cpu_percent: float | None = None
    ram_used_bytes: int | None = None
    ram_total_bytes: int | None = None
    temperature_c: float | None = None
    network_rx_bytes: int | None = None
    network_tx_bytes: int | None = None
    network_operational: bool | None = None
    interfaces: list[str] = field(default_factory=list)


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="ascii")
    except (OSError, UnicodeError):
        return ""


def sample_system(proc: Path, thermal: Path, network: Path) -> SystemSample:
    result = SystemSample(timestamp=datetime.now(UTC))
    cpu = next((row for row in read_text(proc / "stat").splitlines() if row.startswith("cpu ")), "")
    try:
        fields = [int(part) for part in cpu.split()[1:9]]
        if len(fields) >= 4:
            result.cpu_ticks = (sum(fields), sum(fields[3:5]))
    except ValueError:
        pass

    memory = {}
    for row in read_text(proc / "meminfo").splitlines():
        key, _, tail = row.partition(":")
        if key in ("MemTotal", "MemAvailable"):
            try:
                memory[key] = int(tail.split()[0]) * 1024
            except (ValueError, IndexError):
                continue
    if memory.get("MemTotal", 0) > 0:
        result.ram_total_bytes = memory["MemTotal"]
        if "MemAvailable" in memory:
            result.ram_used_bytes = max(
                0, min(memory["MemTotal"], memory["MemTotal"] - memory["MemAvailable"])
            )

    try:
        result.temperature_c = int(read_text(thermal / "thermal_zone0" / "temp")) / 1000
    except ValueError:
        pass

    rx = tx = 0
    for row in read_text(proc / "net" / "dev").splitlines():
        name, _, tail = row.strip().partition(":")
        parts = tail.split()
        if not tail or name.strip() == "lo" or len(parts) < 9:
            continue
        try:
            received, sent = int(parts[0]), int(parts[8])
        except ValueError:
            continue
        result.interfaces.append(name.strip())
        rx += received
        tx += sent
    if result.interfaces:
        result.network_rx_bytes, result.network_tx_bytes = rx, tx
        states = {
            read_text(network / interface / "operstate").strip() for interface in result.interfaces
        }
        if "up" in states:
            result.network_operational = True
        elif states <= {"down", "lowerlayerdown", "dormant", "notpresent", "testing"}:
            result.network_operational = False
    return result


class TelemetrySampler:
    def __init__(
        self,
        proc: Path = Path("/proc"),
        thermal: Path = Path("/sys/class/thermal"),
        network: Path = Path("/sys/class/net"),
    ) -> None:
        self.proc = proc
        self.thermal = thermal
        self.network = network
        self.lock = asyncio.Lock()
        self.cached: SystemSample | None = None
        self.sampled_at = 0.0

    async def sample(self) -> SystemSample:
        async with self.lock:
            if self.cached is not None and monotonic() - self.sampled_at < 0.7:
                return self.cached
            latest = await asyncio.to_thread(sample_system, self.proc, self.thermal, self.network)
            if latest.cpu_ticks and self.cached is not None and self.cached.cpu_ticks:
                total, idle = latest.cpu_ticks
                previous_total, previous_idle = self.cached.cpu_ticks
                difference = total - previous_total
                if difference > 0 and idle >= previous_idle:
                    latest.cpu_percent = round(
                        max(
                            0.0,
                            min(100.0, 100 * (difference - (idle - previous_idle)) / difference),
                        ),
                        1,
                    )
            self.cached = latest
            self.sampled_at = monotonic()
            return latest
