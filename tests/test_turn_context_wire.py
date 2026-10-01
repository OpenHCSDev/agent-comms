"""Original sealed messaging survives silent context rows through every projection."""
from dataclasses import replace
import hashlib
import json

import pytest

from agent_comms.field_codec import FieldCodec
from agent_comms.thread_identity import TurnId, TurnIdentity
from agent_comms.turn_context import ContextManifest, RecordedContextTurn, SegmentManifest, OwnerProvenance
from agent_comms.wire_record import WireRecord, ObservationWireRecord, ContextManifestWireObservation
from agent_comms.cli_commands import ContextCliCommand
from agent_comms.comms import Comms
from test_private_bus_checkpoint import _root, _page
from agent_comms.bus_publication import stable_thread_lookup


def manifest(owner, generation=1):
    source=OwnerProvenance(owner.incarnation, 'original-input-source')
    segment=SegmentManifest('transcript',(source,),hashlib.sha256(b'PRIVATE INPUT').hexdigest(),13,4)
    return ContextManifest(owner.incarnation,RecordedContextTurn(TurnId('original-turn'),TurnIdentity(owner.incarnation,generation)),(segment,),'pi.estimateTokens')


def test_silent_manifest_continuous_original_message_and_cold_projection(tmp_path, monkeypatch):
    comms,root_id=_root(tmp_path)
    first=comms.messaging.send_initial_cohort('sender','#team','@Alice original question')
    lookup=stable_thread_lookup(17002.0)
    prior=_page(comms,lookup)
    raw=comms.bus.log.path.read_bytes()
    activity=comms.bus.channel_activity()
    pending=comms.bus.pending_counts_all(['Alice','Bob'])
    awareness=comms.bus.awareness_prompt(comms.registry.require('Alice'))
    comms.bus.log.record_context(manifest(comms.registry.require('Alice')))
    assert comms.bus.log.path.read_bytes().startswith(raw)
    assert b'PRIVATE INPUT' not in comms.bus.log.path.read_bytes()[len(raw):]
    assert comms.bus.log.latest_sequence()==first.seq
    assert comms.bus.log.total_messages()==1
    assert _page(comms,lookup)[0].offset>prior[0].offset
    assert _page(comms,lookup)[1:]==prior[1:]
    assert comms.bus.channel_activity()==activity
    assert comms.bus.pending_counts_all(['Alice','Bob'])==pending
    assert comms.bus.awareness_prompt(comms.registry.require('Alice'))==awareness
    assert comms.bus.log.full_history()==[first]
    with comms.bus.log.full_history_snapshot() as (_, messages):
        assert list(messages)==[first]
    second=comms.messaging.send_initial_cohort('sender','#team','@Alice next original message')
    assert second.seq==first.seq+1
    comms.bus.log.record_context(manifest(comms.registry.require('Alice'),2))
    reopened=Comms(comms.root)
    assert reopened.bus.log.full_history()==[first,second]
    assert len(_page(reopened,lookup)[1])==2
    monkeypatch.setattr(type(reopened.relationships), "RECENT_MESSAGES", 2)
    recent, limited = reopened.relationships._recent_messages()
    assert recent == (first, second) and not limited
    assert reopened.bus.pending_counts_all(['Alice','Bob'])['Alice']==2
    assert len(ContextCliCommand(thread='Alice',turn=1).apply(reopened)['manifests'])==1
    assert ContextCliCommand(thread='Alice',diff=True).apply(reopened)['turn']['occurrence']['generation']==2


def test_observation_family_rejects_message_fields_and_preview(tmp_path):
    comms,root_id=_root(tmp_path)
    row=ObservationWireRecord(ContextManifestWireObservation(manifest(comms.registry.require('Alice'))))
    assert WireRecord.from_wire(row.to_wire(),root_id).messages()==()
    assert row.sequence_after(39)==39
    with pytest.raises(ValueError):
        WireRecord.from_wire({**row.to_wire(),'seq':40},root_id)
