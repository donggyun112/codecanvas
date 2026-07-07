"""Runtime trace data models."""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any


class TraceEventType(enum.Enum):
    """Supported runtime trace event types."""

    CALL = "call"
    RETURN = "return"
    EXCEPTION = "exception"
    DB = "db"
    HTTP = "http"


@dataclass
class TraceEvent:
    """A single runtime event captured during one traced execution."""

    event_type: TraceEventType
    file_path: str
    func_name: str
    line: int
    timestamp_ns: int
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-friendly shape."""
        return {
            "eventType": self.event_type.value,
            "filePath": self.file_path,
            "funcName": self.func_name,
            "line": self.line,
            "timestampNs": self.timestamp_ns,
            "detail": self.detail,
        }


@dataclass
class TraceResult:
    """The ordered runtime events collected for a single trace session."""

    project_root: str
    started_at_ns: int
    ended_at_ns: int
    events: list[TraceEvent] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def duration_ms(self) -> float:
        """Total trace duration in milliseconds."""
        return (self.ended_at_ns - self.started_at_ns) / 1_000_000

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-friendly shape."""
        return {
            "projectRoot": self.project_root,
            "startedAtNs": self.started_at_ns,
            "endedAtNs": self.ended_at_ns,
            "durationMs": self.duration_ms,
            "events": [event.to_dict() for event in self.events],
            "metadata": self.metadata,
        }
