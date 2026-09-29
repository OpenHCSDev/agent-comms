"""Current saved-state regression after the one-use operator has been deleted.

All live inputs are read-only. Actual copied sources use the current snapshotter;
no retained publication translator, schema repair or alternative reader exists.
"""
from contextlib import closing
import json
import os
from pathlib import Path
import shutil

from agent_comms.comms import wire
from agent_comms.field_codec import FieldCodec
from agent_comms.historical_views import ChannelHistory, HistorySource
from agent_comms.private_bus_checkpoint import _connect, _saved
from agent_comms.wire_log import WireLog


def current_sources():
    live=Path(os.environ['ACTUAL_COMMS_SOURCE_ROOT'])
    return tuple(FieldCodec.decode(HistorySource,value)
                 for value in json.loads((live/'history_sources.json').read_text()))


def test_all_declared_saved_checkpoints_are_current_readonly():
    sources=current_sources()
    assert sources,'Representative retained history is required'
    for source in sources:
        source.validate()
        root=Path(source.root)
        # Exact failing canonical reader, with SQLite's owner-enforced RO mode.
        with closing(_connect(root/'private_bus_checkpoint.sqlite3',readonly=True)) as db:
            witness=_saved(db)
        assert witness.root_id==source.wire_root_id


def test_current_saved_history_has_no_live_admission_or_delivery(tmp_path):
    original=max(current_sources(),key=lambda source:(Path(source.root)/'bus.jsonl').stat().st_size)
    copied=tmp_path/'original'
    shutil.copytree(original.root,copied)
    comms=wire(tmp_path/'destination')
    comms.messaging.initialize_private_initial_protocol()
    source=comms.views.attach_history(copied)
    live_before=(comms.root/'bus.jsonl').read_bytes()
    historical=comms.bus.historical_page(ChannelHistory('#comms',frozenset({'#comms'})))
    assert historical.messages,'Real supplied history must contain #comms messages'
    assert all(not message.starts_turn for message in historical.messages)
    assert all(message.source==source for message in historical.messages)
    assert (comms.root/'bus.jsonl').read_bytes()==live_before
    assert (Path(source.root)/'bus.jsonl').read_bytes()==(copied/'bus.jsonl').read_bytes()
    marker=WireLog(Path(source.root)/'bus.jsonl').read_metadata_unlocked(required=True)
    assert marker.admission_after_seq==marker.last_seq
