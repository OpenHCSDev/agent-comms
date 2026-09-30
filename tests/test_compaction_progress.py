"""One native observation survives automatic/selected publication and phase projection."""

import json

import pytest

from agent_comms import agent_events as events
from agent_comms.acp_extension import CompactionChangedUpdate, decode_updates, encode_updates
from agent_comms.compaction_progress import CompactionSourceProgress
from agent_comms.field_codec import FieldCodec
from agent_comms.pi_rpc import PiRpcChannel
from agent_comms.turn_phase import CompactionPhase, ModelWaitPhase


@pytest.mark.parametrize("automatic", [False, True])
def test_native_progress_roundtrip_and_phase_clock(automatic):
    source = CompactionSourceProgress(500, 1000, "synthesis", 1_000_000, 1_165_000)
    event_type = events.CompactionProgress if automatic else events.CompactionSummaryProgress
    kwargs = {"chunk_index": 2} if automatic else {}
    progress = event_type(operation_id="native-job", text="Summary in progress", source=source,
                          **kwargs)
    (decoded,) = decode_updates(encode_updates(CompactionChangedUpdate(progress)))
    assert decoded.event == progress
    assert isinstance(decoded.event, events.CompactionSummaryProgress)
    phase = ModelWaitPhase().compacting().measured(progress.operation_id, decoded.event.source)
    assert phase.started_at == 1000.0
    assert phase.summary == "Compacting context · 50% of input processed"
    assert phase.source.elapsed_ms == 165_000
    assert phase.compaction_ended() == ModelWaitPhase()
    assert "started_at" not in FieldCodec.encode(phase)
    assert isinstance(FieldCodec.decode(CompactionPhase, FieldCodec.encode(phase)), CompactionPhase)


def test_automatic_native_boundary_keeps_the_nested_measurement():
    source = CompactionSourceProgress(0, 1000, "history", 1000, 1250)
    event = PiRpcChannel.decode_record(json.dumps({
        "type": "compaction_progress", "reason": "threshold", "operationId": "native-job",
        "chunkIndex": 0, "source": FieldCodec.encode(source), "text": "",
    }).encode())
    assert event.source == source
    assert event.source.elapsed_ms == 250


def test_native_timing_rejects_an_observation_before_its_job():
    with pytest.raises(ValueError, match="Invalid native compaction timing"):
        CompactionSourceProgress(0, 1000, "history", 1250, 1000)


def test_stream_measurements_do_not_republish_unchanged_source_state():
    first = CompactionSourceProgress(0, 1000, "map", 1000, 1250)
    heartbeat = CompactionSourceProgress(0, 1000, "map", 1000, 2000)
    completed = CompactionSourceProgress(500, 1000, "map", 1000, 2000)
    phase = ModelWaitPhase().compacting().measured("native-job", first)
    assert heartbeat.elapsed_ms == 1000
    assert phase.measured("native-job", heartbeat) == phase
    assert phase.measured("native-job", completed) != phase
