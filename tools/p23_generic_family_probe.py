"""Check generic declared-family decoding with a working raw-family control."""

from dataclasses import dataclass

from agent_comms.nominal.declared_family import DeclaredFamily
from agent_comms.nominal.field_codec import FieldCodec, UnsupportedFieldType


class ProbeFamily[Payload](DeclaredFamily, affix="Value"):
    """A generic interface; its concrete member keeps a concrete stored field."""


@dataclass(frozen=True)
class ConcreteValue(ProbeFamily[int]):
    value: int


def main() -> None:
    original = ConcreteValue(7)
    encoded = FieldCodec.encode(original)
    assert FieldCodec.decode(ProbeFamily, encoded) == original
    print("Raw declared-family control: round-trip passed")
    try:
        decoded = FieldCodec.decode(ProbeFamily[int], encoded)
    except (UnsupportedFieldType, KeyError, TypeError) as error:
        print(f"Specialized declared-family alias: {type(error).__name__}: {error}")
    else:
        assert decoded == original
        print("Specialized declared-family alias: round-trip passed")


if __name__ == "__main__":
    main()
