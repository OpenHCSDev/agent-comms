"""Current saved-state regression after the one-use operator has been deleted.

All live inputs are read-only. Existing immutable sources retain their original
byte certificates; no archive schema repair or resealing occurs.
"""
import json
import os
from pathlib import Path

from agent_comms.comms import wire
from agent_comms.field_codec import FieldCodec
from agent_comms.historical_views import ChannelHistory, HistorySource
from agent_comms.wire_log import WireLog



def reference_actual_history(destination, live):
    """Borrow the original immutable sources, including their exact seals."""
    sources=tuple(FieldCodec.decode(HistorySource, raw)
                  for raw in json.loads((live/'history_sources.json').read_text()))
    originals={}
    for source in sources:
        source.validate()
        original=Path(source.root)
        originals[original]=(original,(original/'bus.jsonl').read_bytes(),
                             (original/'private_bus_checkpoint.sqlite3').read_bytes())
    (destination/'history_sources.json').write_text(json.dumps([FieldCodec.encode(s) for s in sources]))
    return originals


def current_sources():
    live=Path(os.environ['ACTUAL_COMMS_SOURCE_ROOT'])
    return tuple(FieldCodec.decode(HistorySource,value)
                 for value in json.loads((live/'history_sources.json').read_text()))


def test_all_declared_saved_checkpoints_keep_original_certificates():
    sources=current_sources()
    assert sources,'Representative retained history is required'
    for source in sources:
        source.validate()
        root=Path(source.root)
        # The canonical read uses archived access and the original sealed DB.
        with WireLog(root/'bus.jsonl').certified_read() as opened:
            witness=opened.witness
            assert opened.connection.execute('PRAGMA query_only').fetchone()[0]==1
        assert witness.root_id==source.wire_root_id


def test_current_saved_history_has_no_live_admission_or_delivery(tmp_path):
    original=max(current_sources(),key=lambda source:(Path(source.root)/'bus.jsonl').stat().st_size)
    comms=wire(tmp_path/'destination')
    comms.messaging.initialize_private_initial_protocol()
    reference_actual_history(comms.root, Path(os.environ['ACTUAL_COMMS_SOURCE_ROOT']))
    source=max(comms.bus.history.sources(),key=lambda source:source.size)
    live_before=(comms.root/'bus.jsonl').read_bytes()
    historical=comms.bus.history.page(ChannelHistory('#comms',frozenset({'#comms'})))
    assert historical.messages,'Real supplied history must contain #comms messages'
    assert all(not message.starts_turn for message in historical.messages)
    assert all(message.source==source for message in historical.messages)
    assert (comms.root/'bus.jsonl').read_bytes()==live_before
    assert (Path(source.root)/'bus.jsonl').read_bytes()==(Path(original.root)/'bus.jsonl').read_bytes()
    marker=WireLog(Path(source.root)/'bus.jsonl').read_metadata_unlocked(required=True)
    assert marker.admission_after_seq==marker.last_seq
