"""Cursor owners preserve source identity and the existing CLI JSON forms."""
import json

import pytest

from agent_comms.cli import build_parser
from agent_comms.cli_commands import CliCommand
from agent_comms.field_codec import FieldCodec
from agent_comms.page_traversal import (
    HistoryCursor, LiveCursor, NewerTraversal, OlderTraversal, PageCursor,
)


@pytest.mark.parametrize('cursor,wire,source', [
    (LiveCursor(12), 12, None),
    (HistoryCursor('saved', 12), {'source': 'saved', 'sequence': 12}, 'saved'),
])
def test_cursor_owns_traversal_and_cli_json(cursor, wire, source):
    assert cursor.older() == OlderTraversal(12, source)
    assert cursor.newer() == NewerTraversal(12, source)
    assert FieldCodec.encode(cursor) == wire
    assert FieldCodec.decode(PageCursor, wire) == cursor
    args = build_parser().parse_args(['history', '--before', json.dumps(wire)])
    command = CliCommand.decode(args.command).from_namespace(args)
    assert command.before == cursor


@pytest.mark.parametrize('wire', [True, False, 1.5, '12', [], {},
    {'source': True, 'sequence': 12}, {'source': 'saved', 'sequence': True}])
def test_cursor_rejects_other_json(wire):
    with pytest.raises((ValueError, TypeError)):
        FieldCodec.decode(PageCursor, wire)
