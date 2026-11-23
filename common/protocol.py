import re
from datetime import datetime
from typing import Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from common.parameters import Parameter, Value

PROTOCOL_VERSION = 1
LOG_LIMIT = 300

RunStatus = Literal["running", "stopping", "succeeded", "stopped", "failed"]
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR"]
LogSource = Literal["stdout", "stderr", "agent"]


def valid_token(token: str) -> bool:
    return re.fullmatch(r"[A-Za-z0-9_-]{32,256}", token) is not None


class Health(BaseModel):
    protocol: int
    robot: Literal["mock", "yahboom"]
    camera_available: bool = False
    observability_available: bool = False


class Script(BaseModel):
    id: str
    name: str
    description: str
    entrypoint: str | None = None
    requires_heartbeat: bool = False
    parameters: list[Parameter] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_parameters(self) -> Self:
        fields: dict[str, Parameter] = {}
        flags: set[str] = set()
        for parameter in self.parameters:
            if parameter.key in fields or parameter.flag in flags:
                raise ValueError("The manifest contains a duplicate parameter or flag")
            for key, expected in parameter.requires.items():
                if key not in fields:
                    raise ValueError(f"Dependency {key} must precede {parameter.key}")
                fields[key].validate_value(expected)
            fields[parameter.key] = parameter
            flags.add(parameter.flag)
        return self

    def configuration(self, supplied: dict[str, Value]) -> dict[str, Value]:
        unknown = supplied.keys() - {parameter.key for parameter in self.parameters}
        if unknown:
            raise ValueError(f"Неизвестные параметры: {', '.join(sorted(unknown))}.")
        values: dict[str, Value] = {}
        for parameter in self.parameters:
            if parameter.enabled(values):
                value = supplied.get(parameter.key, parameter.default)
                values[parameter.key] = parameter.validate_value(value)
            elif parameter.key in supplied:
                raise ValueError(f"«{parameter.title}» недоступен для выбранного сценария.")
        return values

    def arguments(self, supplied: dict[str, Value]) -> list[str]:
        values = self.configuration(supplied)
        arguments = []
        for parameter in self.parameters:
            if parameter.key not in values:
                continue
            value = values[parameter.key]
            if parameter.kind == "bool":
                if value:
                    arguments.append(parameter.flag)
            else:
                arguments.append(f"{parameter.flag}={value}")
        return arguments


class StartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    script_id: str
    parameters: dict[str, Value] = Field(default_factory=dict, max_length=64)
    supports_lease: bool = False


class RunState(BaseModel):
    id: UUID
    script_id: str
    status: RunStatus
    pid: int | None
    started_at: datetime
    elapsed_seconds: float
    exit_code: int | None


class RunStarted(RunState):
    lease: UUID | None = None


class Heartbeat(BaseModel):
    lease: UUID


class SafetyState(BaseModel):
    latched: bool = False
    motor_disable_confirmed: bool | None = None
    reason: str | None = None


class LogLine(BaseModel):
    sequence: int
    timestamp: datetime
    source: LogSource
    level: LogLevel
    message: str
    event: str | None = None


class EventFrame(BaseModel):
    run: RunState | None
    logs: list[LogLine]
    gap: bool = False
    safety: SafetyState | None = None


CameraView = Literal["original", "gray", "mask", "overlay"]


class CameraOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    view: CameraView = "original"
    fps: int = Field(default=5, ge=1, le=8, strict=True)
    quality: int = Field(default=75, ge=35, le=90, strict=True)


class CameraStarted(BaseModel):
    id: UUID


class MetricSample(BaseModel):
    name: str
    value: float
    timestamp: datetime


class Experiment(BaseModel):
    id: UUID
    script_id: str
    script_name: str
    parameters: dict[str, Value] = Field(default_factory=dict)
    started_at: datetime
    finished_at: datetime | None = None
    status: RunStatus | Literal["interrupted"]
    elapsed_seconds: float
    exit_code: int | None = None
    metrics: dict[str, float] = Field(default_factory=dict)


class ExperimentDetails(BaseModel):
    run: Experiment
    logs: list[LogLine] = Field(default_factory=list)
    samples: list[MetricSample] = Field(default_factory=list)


class SystemTelemetry(BaseModel):
    timestamp: datetime
    cpu_percent: float | None = None
    ram_used_bytes: int | None = None
    ram_total_bytes: int | None = None
    temperature_c: float | None = None
    network_rx_bytes: int | None = None
    network_tx_bytes: int | None = None
    network_operational: bool | None = None
    interfaces: list[str] = Field(default_factory=list)
    robot: Literal["mock", "yahboom"]
    camera_available: bool = False
    hardware_busy: bool = False
    run_id: UUID | None = None
    metrics: dict[str, float] = Field(default_factory=dict)
