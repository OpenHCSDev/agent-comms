"""Human-readable declared values shared by heartbeat text and native views."""

from __future__ import annotations

from dataclasses import dataclass, is_dataclass
from functools import singledispatchmethod

from agent_comms.declared_family import FamilyMeta
from agent_comms.field_codec import DisclosureIdentity, FieldCodec


@dataclass(frozen=True)
class DisclosureText:
    value: object

    @singledispatchmethod
    @classmethod
    def of(cls, value: object) -> DisclosureText:
        """Recover foreign record shape once, at this presentation boundary."""
        if is_dataclass(value):
            return RecordText(value)
        return cls(value)

    @of.register(FamilyMeta)
    @classmethod
    def _family(cls, value: FamilyMeta) -> DisclosureText:
        return FamilyText(value)

    def summary(self, width: int | None = None) -> str:
        return str(self.value)[:width].partition("\n")[0]

    @classmethod
    def view(cls, value: object, view: str) -> tuple[str, ...]:
        """Read the original values, not their flattened JSON representations."""
        return (
            *(cls.of(plan.current(value)).summary()
              for plan in FieldCodec.field_plans(type(value))
              if plan.records and not plan.excluded_in(view)),
            *(cls.of(getattr(value, name)).summary()
              for name in FieldCodec.projections(type(value), view)),
        )


class RecordText(DisclosureText):
    def summary(self, width: int | None = None) -> str:
        plans = tuple(plan for plan in FieldCodec.field_plans(type(self.value)) if plan.records)
        identity = tuple(plan for plan in plans if DisclosureIdentity.read(plan.annotation, False))
        parts: list[str] = []
        for plan in identity or plans:
            parts.append(DisclosureText.of(plan.current(self.value)).summary(width))
            if width is not None and sum(map(len, parts)) >= width:
                break
        return " · ".join(parts)[:width]


@dataclass(frozen=True)
class FamilyText(DisclosureText):
    value: FamilyMeta

    def summary(self, width: int | None = None) -> str:
        return self.value.declared_name[:width]
