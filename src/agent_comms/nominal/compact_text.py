"""Shared compact text formatting for model-facing tool replies and domain records.

Every ``DeclaredReply`` (all ``ToolResult`` and ``CliResult`` subclasses) inherits
``CompactText`` so MCP and agent-facing outputs render compact, unescaped, noise-free
text instead of dumping nested wire JSON into a model's working memory. Domain value
types (messages, references, threads, channels, goals) override ``compact_text()`` once
and are formatted identically everywhere they appear.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping, Sequence
from typing import ClassVar

from metaclass_registry.mro_dispatch import MroDispatch, handles

from agent_comms.nominal.field_codec import FieldCodec, NullMember, WireExclude

_NOISE_KEYS: frozenset[str] = frozenset({"agentCommsKind", "agent_comms_kind"})
_TRIVIAL_KINDS: frozenset[str] = frozenset({
    "none",
    "default",
    "complete",
    "unordered",
    "unreported",
    "no_goal",
    "absent",
})


class CompactText:
    """Inherited compact text rendering for replies and domain values."""

    __slots__ = ()

    def compact_text(self) -> str:
        """Render this object compactly for a model or human reader."""
        return COMPACT_RENDERER.render_record(self)


def _is_empty(value: object) -> bool:
    if value is None or isinstance(value, NullMember):
        return True
    if isinstance(value, str):
        return not value
    if isinstance(value, (list, tuple, set, frozenset, dict, Mapping)):
        return len(value) == 0
    return False


def _is_trivial_wire_dict(value: Mapping[object, object]) -> bool:
    if not value:
        return True
    if len(value) == 1 and "kind" in value and value["kind"] in _TRIVIAL_KINDS:
        return True
    return False


def _indent(text: str, prefix: str = "  ") -> str:
    return "\n".join(f"{prefix}{line}" if line else "" for line in text.splitlines())


class CompactRenderer(MroDispatch):
    """Single shared recursive formatter for all ``CompactText`` subclasses and wire values."""

    max_inline_items: ClassVar[int] = 8

    def render(self, value: object) -> str:
        return self.handler_for(type(value))(self, value)

    def render_record(self, record: object) -> str:
        """Render a dataclass or declared reply by walking its live fields so nested
        ``CompactText`` children format through their own ``compact_text()`` methods."""
        if not dataclasses.is_dataclass(record) or isinstance(record, type):
            to_wire = getattr(record, "to_wire", None)
            if callable(to_wire):
                return self.render(to_wire())
            return str(record)
        plans = {plan.name: plan for plan in FieldCodec.field_plans(type(record))}
        lines: list[str] = []
        for field in dataclasses.fields(record):
            if field.name in _NOISE_KEYS:
                continue
            plan = plans.get(field.name)
            if plan is not None and plan.setting(WireExclude, False):
                continue
            value = getattr(record, field.name)
            if _is_empty(value):
                continue
            rendered = self.render(value)
            if not rendered:
                continue
            key = plan.key if plan is not None else field.name
            if key in _NOISE_KEYS:
                continue
            if "\n" in rendered:
                lines.append(f"{key}:\n{_indent(rendered)}")
            else:
                lines.append(f"{key}: {rendered}")
        return "\n".join(lines) if lines else type(record).__name__

    @handles(CompactText)
    def _compact_text(self, value: CompactText) -> str:
        return value.compact_text()

    @handles(NullMember)
    def _null_member(self, value: NullMember) -> str:
        return ""

    @handles(bool)
    def _bool(self, value: bool) -> str:
        return "true" if value else "false"

    @handles(int)
    def _int(self, value: int) -> str:
        return str(value)

    @handles(float)
    def _float(self, value: float) -> str:
        return f"{value:g}"

    @handles(str)
    def _str(self, value: str) -> str:
        return value

    @handles(type)
    def _type(self, value: type) -> str:
        return str(getattr(value, "declared_name", None) or value.__name__)

    @handles(dict)
    @handles(Mapping)
    def _mapping(self, value: Mapping[object, object]) -> str:
        if _is_trivial_wire_dict(value):
            return ""
        lines: list[str] = []
        for raw_key, item in value.items():
            key = str(raw_key)
            if key in _NOISE_KEYS or _is_empty(item):
                continue
            if isinstance(item, Mapping) and _is_trivial_wire_dict(item):
                continue
            rendered = self.render(item)
            if not rendered:
                continue
            if "\n" in rendered:
                lines.append(f"{key}:\n{_indent(rendered)}")
            else:
                lines.append(f"{key}: {rendered}")
        return "\n".join(lines)

    @handles(tuple)
    @handles(list)
    @handles(Sequence)
    def _sequence(self, value: Sequence[object]) -> str:
        items = [rendered for item in value if not _is_empty(item) and (rendered := self.render(item))]
        if not items:
            return ""
        if len(items) <= self.max_inline_items and all(
            "\n" not in item and len(item) <= 40 for item in items
        ):
            return ", ".join(items)
        if any("\n" in item for item in items):
            return "\n\n".join(items)
        return "\n".join(f"- {item}" for item in items)

    @handles(frozenset)
    @handles(set)
    def _set(self, value: set[object] | frozenset[object]) -> str:
        items = sorted(
            rendered for item in value if not _is_empty(item) and (rendered := self.render(item))
        )
        return ", ".join(items)

    @handles(object)
    def _object(self, value: object) -> str:
        if dataclasses.is_dataclass(value) and not isinstance(value, type):
            return self.render_record(value)
        return str(value)


COMPACT_RENDERER = CompactRenderer()


def compact_format(value: object) -> str:
    """Format any reply or value using the shared compact renderer."""
    return COMPACT_RENDERER.render(value)
