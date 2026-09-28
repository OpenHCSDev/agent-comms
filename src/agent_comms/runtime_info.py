"""Runtime info: declaration and persistence owners."""

from __future__ import annotations

import json
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from .errors import RelationViolationError
from .store_files import _atomic_write_text, _store_lock


@dataclass(frozen=True, slots=True)
class AgentRuntimeInfo:
    """Declares mutable runtime metadata for one registered thread."""

    thread: str
    model: str | None = None
    session_name: str | None = None
    context_used: int | None = None
    context_size: int | None = None
    timestamp: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        if not self.thread:
            raise RelationViolationError("Runtime-info thread cannot be empty.")
        if self.context_used is not None and self.context_used < 0:
            raise ValueError("Context usage cannot be negative.")
        if self.context_size is not None and self.context_size < 0:
            raise ValueError("Context size cannot be negative.")

    @property
    def context_percent(self) -> float | None:
        if self.context_used is None or not self.context_size:
            return None
        return self.context_used / self.context_size * 100

    @property
    def context_label(self) -> str:
        if self.context_percent is None:
            return ""
        return f"{self.context_used:,}/{self.context_size:,} tokens ({self.context_percent:.1f}%)"

    def to_wire(self) -> dict:
        return {
            "thread": self.thread,
            "model": self.model,
            "session_name": self.session_name,
            "context_used": self.context_used,
            "context_size": self.context_size,
            "ts": self.timestamp,
        }

    @classmethod
    def from_wire(cls, data: Mapping) -> AgentRuntimeInfo:
        return cls(
            thread=data["thread"],
            model=data.get("model"),
            session_name=data.get("session_name"),
            context_used=data.get("context_used"),
            context_size=data.get("context_size"),
            timestamp=data.get("ts", 0.0),
        )


class RuntimeInfoStore:
    """Persists the latest runtime metadata for each thread."""

    def __init__(self, store_path: Path):
        self._path = store_path

    def set(self, info: AgentRuntimeInfo) -> None:
        with _store_lock(self._path):
            values = self._load_unlocked()
            values[info.thread] = info
            _atomic_write_text(
                self._path,
                json.dumps({name: value.to_wire() for name, value in values.items()}, indent=2),
            )

    def get(self, thread: str) -> AgentRuntimeInfo | None:
        return self._load().get(thread)

    def all(self) -> Mapping[str, AgentRuntimeInfo]:
        return self._load()

    def remove(self, thread: str) -> None:
        with _store_lock(self._path):
            values = self._load_unlocked()
            values.pop(thread, None)
            _atomic_write_text(
                self._path,
                json.dumps({name: value.to_wire() for name, value in values.items()}, indent=2),
            )

    def rename_thread(self, old_name: str, new_name: str) -> None:
        with _store_lock(self._path):
            values = self._load_unlocked()
            if info := values.pop(old_name, None):
                values[new_name] = AgentRuntimeInfo(
                    thread=new_name,
                    model=info.model,
                    session_name=info.session_name,
                    context_used=info.context_used,
                    context_size=info.context_size,
                    timestamp=info.timestamp,
                )
            _atomic_write_text(
                self._path,
                json.dumps({name: value.to_wire() for name, value in values.items()}, indent=2),
            )

    def _load(self) -> dict[str, AgentRuntimeInfo]:
        with _store_lock(self._path):
            return self._load_unlocked()

    def _load_unlocked(self) -> dict[str, AgentRuntimeInfo]:
        if not self._path.exists():
            return {}
        raw = json.loads(self._path.read_text())
        return {name: AgentRuntimeInfo.from_wire(data) for name, data in raw.items()}
