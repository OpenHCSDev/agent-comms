"""Run the source-backed carry on a SQLite snapshot, preserving the live store."""
from __future__ import annotations

import json
import sys
from contextlib import closing
from pathlib import Path

from agent_comms.commander.order import COMMS
from agent_comms.coordination_database import CoordinationStore
from agent_comms.external_transcript import ExternalEventRow, ExternalTranscript
from agent_comms.field_codec import FieldCodec
from agent_comms.transcript_events import TranscriptEvent
from cutover.carry_external_event_times import ExternalEventTimes


root = Path(sys.argv[1])
root.mkdir(parents=True, exist_ok=False)
source = ExternalTranscript.path_in(COMMS.root)
copy = root / source.name
with closing(CoordinationStore.read_snapshot(source)) as reader:
    with closing(CoordinationStore.writer(copy)) as writer:
        reader.backup(writer)
with CoordinationStore.reading(copy) as connection:
    before, = connection.execute(
        f'SELECT count(*) FROM "{ExternalEventRow.declared_name}" WHERE timestamp IS NULL'
    ).fetchone()
print('copied undated rows:', before, flush=True)
result = ExternalEventTimes.carry(root, apply=True)
print(json.dumps(result.report('carry_external_event_times', True)), flush=True)
assert ExternalEventTimes.carry(root, apply=True).changed == 0
count = 0
with CoordinationStore.reading(copy) as connection:
    for encoded, in connection.execute(f'SELECT event FROM "{ExternalEventRow.declared_name}"'):
        event = FieldCodec.decode(TranscriptEvent, FieldCodec.parse(encoded))
        assert isinstance(event.timestamp, float)
        count += 1
print('all copied events decoded with recorded float timestamps:', count)
print('source-backed carry idempotent; live store opened read-only')
