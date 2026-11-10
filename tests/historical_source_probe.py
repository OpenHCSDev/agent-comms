"""Decode original imported metadata cold, without importing its concrete source producer."""

import json
import sys

from agent_comms.execution.imported_session_metadata import ImportedSessionMetadata
from agent_comms.nominal.field_codec import FieldCodec


def main() -> None:
    wire = json.loads(sys.argv[1])
    metadata = FieldCodec.decode(ImportedSessionMetadata, wire)
    assert FieldCodec.encode(metadata) == wire
    for source in metadata.historical_instructions:
        assert source.public_text(None) == sys.argv[2]
    print("Original historical metadata decoded cold and authenticated its saved source")


if __name__ == "__main__":
    main()
