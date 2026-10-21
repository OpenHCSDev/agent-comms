"""One-shot P380 cutover, with the connector stopped and grants.json backed up.

PYTHONPATH=src python tools/cutover/connector_browser_trust.py PATH/TO/grants.json

The installer operator deletes this tool after the live cutover. It is deliberately
not a resident reader and does not infer browser trust from an old client approval.
"""
from __future__ import annotations

import argparse
from pathlib import Path

from agent_comms.connector_store import ConnectorGrants, ConnectorStore
from agent_comms.field_codec import FieldCodec


def convert(store: ConnectorStore) -> None:
    """Discard waiting requests, retaining every client/code/access/refresh grant."""
    with store.locked():
        grants = store._read_using(
            lambda raw: FieldCodec.decode_document(ConnectorGrants, {**raw, "requests": {}})
        )
        store._publish_unlocked(grants)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path)
    arguments = parser.parse_args()
    convert(ConnectorStore(arguments.path))


if __name__ == "__main__":
    main()
