"""Editable fields inherited by their actual records and command addresses."""

from dataclasses import dataclass, replace
from typing import Self

from agent_comms.nominal.field_codec import FieldCodec


@dataclass(frozen=True, kw_only=True)
class ConfigurationRecord:
    """Project and apply exactly the fields this configuration declares."""

    @classmethod
    def field_values(cls, record: object) -> dict[str, object]:
        return {plan.name: plan.current(record) for plan in FieldCodec.field_plans(cls)}

    @classmethod
    def capture(cls, record: object) -> Self:
        return cls(**cls.field_values(record))

    def applied_to[Record: ConfigurationRecord](self, record: Record) -> Record:
        return replace(record, **type(self).field_values(self))
