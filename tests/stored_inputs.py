import json
"""Test inspection of every stored input row; production reads select by index."""

import sqlite3
from contextlib import closing
from pathlib import Path

from agent_comms.input_disposition import InputDispositionRow, InputDispositions, InputDocument


def stored_inputs(root: Path) -> InputDocument:
    with InputDispositions(root).snapshot() as connection:
        return InputDocument({row.key: row.attempt for row in InputDispositionRow.select(connection)})


def insert_inputs(root: Path, rows: dict[str, dict]) -> None:
    """Seed already-stored rows in one transaction, as a previous process left them."""
    InputDispositions(Path(root)).record_originals()  # installs the declared schema
    with closing(sqlite3.connect(InputDispositions.path_in(root))) as connection, connection:
        connection.executemany('INSERT INTO "input_disposition" ("key", "attempt") VALUES (?, ?)',
                               [(key, json.dumps(row)) for key, row in rows.items()])


def sent_text(native, row) -> str:
    """The native session's user text that a bound row's digest proves was sent."""
    for message in native.saved_inputs():
        content = message["content"]
        text = content if isinstance(content, str) else "".join(
            part["text"] for part in content if part.get("type") == "text")
        if row.sent_digest.matches(text):
            return text
    raise AssertionError(f"no native user input matches {row.key}")
