"""Client-safe wire records and scope identity for scoped agent settings.

Kept free of ``objectstate`` and ``rule_deal`` so client/Android and CLI import paths
stay light while sharing the exact ``ScopeKey`` hierarchy and wire payload with Core.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Collection
from dataclasses import dataclass, field
from typing import ClassVar

from pydantic import JsonValue

from agent_comms.nominal.field_codec import boundary_decoder
from agent_comms.transport.tool_result import ToolResult

SCOPE_SEPARATOR = "::"


def _require_segment(name: str, value: str) -> str:
    stripped = value.strip()
    if not stripped or SCOPE_SEPARATOR in stripped:
        raise ValueError(f"Invalid agent settings scope {name}: {value!r}")
    return stripped


class AgentSettingsScope(ABC):
    """One scope in the ``Global -> Project -> [Channel] -> Thread`` inheritance hierarchy.

    ``scope_id`` uses ``objectstate.ScopeKey``'s ``"::"`` segment convention directly so
    both Core and Toad share ``parent`` / ``ancestors()`` with zero translation glue.
    """

    __slots__ = ()
    separator: ClassVar[str] = SCOPE_SEPARATOR

    @property
    @abstractmethod
    def scope_id(self) -> str: ...

    def ancestry(self, *, channel: str = "") -> tuple[AgentSettingsScope, ...]:
        """Ordered scopes from global root down to this scope (inclusive)."""
        del channel
        return (GlobalSettingsScope(), self)

    def owned_by_any(self, thread_names: Collection[str]) -> bool:
        return False

    def renamed_thread(self, old_name: str, new_name: str) -> AgentSettingsScope:
        return self

    def build_layer[Layer](
        self,
        *,
        for_global: Callable[[], Layer],
        for_scoped: Callable[[], Layer],
    ) -> Layer:
        return for_scoped()

    @classmethod
    @boundary_decoder
    def parse(cls, scope_id: str) -> AgentSettingsScope:
        """Decode a ``ScopeKey`` string into its nominal scope member."""
        if scope_id == "":
            return GlobalSettingsScope()
        parts = tuple(scope_id.split(cls.separator))
        if any(not part for part in parts):
            raise ValueError(f"Malformed agent settings scope: {scope_id!r}")
        match parts:
            case ("project", project):
                return ProjectSettingsScope(project)
            case ("project", project, "channel", channel):
                return ChannelSettingsScope(project, channel)
            case ("project", project, "thread", thread):
                return ThreadSettingsScope(project, thread)
            case ("agent", thread):
                return AgentOnlySettingsScope(thread)
        raise ValueError(f"Unknown agent settings scope shape: {scope_id!r}")


@dataclass(frozen=True, slots=True)
class GlobalSettingsScope(AgentSettingsScope):
    """Process-wide global defaults (``ScopeKey("")``)."""

    @property
    def scope_id(self) -> str:
        return ""

    def ancestry(self, *, channel: str = "") -> tuple[AgentSettingsScope, ...]:
        del channel
        return (self,)

    def build_layer[Layer](
        self,
        *,
        for_global: Callable[[], Layer],
        for_scoped: Callable[[], Layer],
    ) -> Layer:
        return for_global()


@dataclass(frozen=True, slots=True)
class ProjectSettingsScope(AgentSettingsScope):
    """Overrides shared by every channel and thread in one project."""

    project: str

    def __post_init__(self) -> None:
        _require_segment("project", self.project)

    @property
    def scope_id(self) -> str:
        return self.separator.join(("project", self.project))


@dataclass(frozen=True, slots=True)
class ChannelSettingsScope(AgentSettingsScope):
    """Overrides for one channel within a project."""

    project: str
    channel: str

    def __post_init__(self) -> None:
        _require_segment("project", self.project)
        _require_segment("channel", self.channel)

    @property
    def scope_id(self) -> str:
        return self.separator.join(("project", self.project, "channel", self.channel))

    def ancestry(self, *, channel: str = "") -> tuple[AgentSettingsScope, ...]:
        del channel
        return (GlobalSettingsScope(), ProjectSettingsScope(self.project), self)


class ThreadOwnedScope(AgentSettingsScope):
    """A scope owned by one thread declaration; deleted or renamed with that thread."""

    __slots__ = ()
    thread: str

    def owned_by_any(self, thread_names: Collection[str]) -> bool:
        return self.thread in thread_names


@dataclass(frozen=True, slots=True)
class ThreadSettingsScope(ThreadOwnedScope):
    """Overrides for one thread within a project."""

    project: str
    thread: str

    def __post_init__(self) -> None:
        _require_segment("project", self.project)
        _require_segment("thread", self.thread)

    @property
    def scope_id(self) -> str:
        return self.separator.join(("project", self.project, "thread", self.thread))

    def ancestry(self, *, channel: str = "") -> tuple[AgentSettingsScope, ...]:
        layers: list[AgentSettingsScope] = [
            GlobalSettingsScope(),
            ProjectSettingsScope(self.project),
        ]
        if channel:
            layers.append(ChannelSettingsScope(self.project, channel))
        layers.append(self)
        return tuple(layers)

    def renamed_thread(self, old_name: str, new_name: str) -> AgentSettingsScope:
        return ThreadSettingsScope(self.project, new_name) if self.thread == old_name else self


@dataclass(frozen=True, slots=True)
class AgentOnlySettingsScope(ThreadOwnedScope):
    """Overrides for a global-only thread without a project scope."""

    thread: str

    def __post_init__(self) -> None:
        _require_segment("thread", self.thread)

    @property
    def scope_id(self) -> str:
        return self.separator.join(("agent", self.thread))

    def renamed_thread(self, old_name: str, new_name: str) -> AgentSettingsScope:
        return AgentOnlySettingsScope(new_name) if self.thread == old_name else self


@dataclass(frozen=True)
class ResolvedSetting:
    """One resolved agent setting with its ``objectstate`` provenance."""

    value: JsonValue
    source_scope: str
    source_type: str
    authored: bool


@dataclass(frozen=True)
class ScopedSettingsProjection(ToolResult):
    """Typed wire projection of one scope's authored ancestor layers and resolved fields.

    ``layers`` carries each scope's authored overrides keyed by exact ``GlobalAgentSettings``
    dotted field paths (absent/``None`` means inherit), so Toad hydrates real ``ObjectState``
    instances with ``parent_state`` directly. ``resolved`` carries Core's effective value and
    provenance per field for fast read-only rendering without re-resolving per frame.
    """

    scope: str
    ancestry: tuple[str, ...]
    layers: dict[str, dict[str, JsonValue]]
    resolved: dict[str, ResolvedSetting]


@dataclass(frozen=True)
class AgentSettingsDocument:
    """Persisted authored-only overrides keyed by ``ScopeKey`` string."""

    scopes: dict[str, dict[str, JsonValue]] = field(default_factory=dict)

    def overrides_at(self, scope_id: str) -> dict[str, JsonValue]:
        return dict(self.scopes.get(scope_id, {}))

    def with_scope_overrides(
        self, scope_id: str, values: dict[str, JsonValue]
    ) -> AgentSettingsDocument:
        updated = dict(self.scopes)
        if values:
            updated[scope_id] = dict(values)
        else:
            updated.pop(scope_id, None)
        return AgentSettingsDocument(updated)

    def without_threads(self, names: Collection[str]) -> AgentSettingsDocument:
        if not names:
            return self
        kept: dict[str, dict[str, JsonValue]] = {}
        for scope_id, values in self.scopes.items():
            parsed = AgentSettingsScope.parse(scope_id)
            if not parsed.owned_by_any(names):
                kept[scope_id] = values
        return AgentSettingsDocument(kept)

    def renamed_thread(self, old_name: str, new_name: str) -> tuple[AgentSettingsDocument, int]:
        if old_name == new_name:
            return (self, 0)
        renamed: dict[str, dict[str, JsonValue]] = {}
        changed = 0
        for scope_id, values in self.scopes.items():
            parsed = AgentSettingsScope.parse(scope_id)
            target = parsed.renamed_thread(old_name, new_name)
            if target.scope_id != scope_id:
                changed += 1
            renamed[target.scope_id] = values
        return (AgentSettingsDocument(renamed), changed)
