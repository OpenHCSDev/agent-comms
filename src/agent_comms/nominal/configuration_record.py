"""Editable fields inherited by their actual records and command addresses."""

from dataclasses import dataclass, replace
from typing import Self

from agent_comms.nominal.field_codec import FieldCodec


@dataclass(frozen=True, kw_only=True)
class ConfigurationRecord:
    """Project and apply exactly the fields this configuration declares."""

    @classmethod
    def capture(cls, record: object) -> Self:
        return cls(**{plan.name: plan.current(record) for plan in FieldCodec.field_plans(cls)})

    def applied_to[Record: ConfigurationRecord](self, record: Record) -> Record:
        return replace(record, **{plan.name: plan.current(self)
                                  for plan in FieldCodec.field_plans(type(self))})
