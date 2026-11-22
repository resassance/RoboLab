import re
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

PROTOCOL_VERSION = 1
LOG_LIMIT = 300

RunStatus = Literal["running", "stopping", "succeeded", "stopped", "failed"]
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR"]
LogSource = Literal["stdout", "stderr", "agent"]


def valid_token(token: str) -> bool:
    return re.fullmatch(r"[A-Za-z0-9_-]{32,256}", token) is not None


class Health(BaseModel):
    protocol: int
    robot: Literal["mock"]


class Script(BaseModel):
    id: str
    name: str
    description: str


class StartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    script_id: str


class RunState(BaseModel):
    id: UUID
    script_id: str
    status: RunStatus
    pid: int | None
    started_at: datetime
    elapsed_seconds: float
    exit_code: int | None


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
