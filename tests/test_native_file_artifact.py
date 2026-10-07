"""Original local write -> saved pair -> witnessed retained cut, without a provider."""
import json
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from agent_comms.compaction_boundary import CompactionBoundary
from agent_comms.field_codec import FieldCodec
from agent_comms.input_disposition import FutureInputQueue, InputDispositions
from agent_comms.native_entries import NativeEntry
from agent_comms.private_path import FileRevision
from agent_comms.owner_compaction_prepare import NativeWitness
from agent_comms.pi_payloads import FileMutationToolDetails, NativeToolDetails
from agent_comms.retained_task_facts import NativeArtifactTaskFact, RetainedTaskFacts
from agent_comms.selected_source import SessionRevision
from agent_comms.text_digest import TextDigest

from test_task_decisions import admit


def test_original_file_operation_survives_later_file_change_and_branch_cut(comms, tmp_path):
    comms.messaging.initialize_private_initial_protocol()
    module = Path(__file__).resolve().parents[1] / 'stack/native-file-artifact.mjs'
    target = tmp_path / 'artifact.py'
    text = 'original = "\u03bb"\n'
    producer = subprocess.run(['node', '--input-type=module', '-e', '''
        const {CompletedFileMutation} = await import(process.argv[1]);
        const original = await CompletedFileMutation.write(process.argv[2], process.argv[3]);
        console.log(JSON.stringify(original.details(undefined)));
    ''', module.as_uri(), str(target), text], check=True, capture_output=True, text=True)
    details = json.loads(producer.stdout)
    assert target.read_bytes() == text.encode()
    target.write_text('later = "different contents"\n')
    # A later filesystem value cannot rewrite what the original successful tool did.
    assert details['agentCommsArtifact']['digest'] == FieldCodec.encode(TextDigest.of(text))
    assert details['agentCommsArtifact']['byte_count'] == len(text.encode())
    decoded = NativeToolDetails.from_wire(details)
    assert isinstance(decoded, FileMutationToolDetails)
    assert decoded.to_wire() == details
    # Extension metadata owns no artifact claim even if it uses a similar key.
    for external in (['opaque', details], 3, 'opaque',
                     {'agentCommsArtifact': details['agentCommsArtifact']}):
        assert NativeToolDetails.from_wire(external).artifacts() == ()
    for malformed in ({**details, NativeToolDetails.wire_tag: 'unknown_owned_result'},
                      {**details, 'unowned_extra_field': True}):
        with pytest.raises(ValueError):
            NativeToolDetails.from_wire(malformed)

    directory = tmp_path / 'native'
    directory.mkdir(mode=0o700)
    saved = directory / 'source.jsonl'
    header = {'type':'session', 'id':'original-session', 'version':3, 'cwd':str(tmp_path)}
    def message(identity, parent, payload):
        return {'type':'message','id':identity,'parentId':parent,'message':payload}
    user = message('user', None, {'role':'user','content':[{'type':'text','text':'Write exact artifact'}]})
    def request(identity, parent):
        return message(identity, parent, {'role':'assistant','content':[
            {'type':'toolCall','id':'reused-call','name':'write',
             'arguments':{'path':str(target),'content':text}}]})
    def result(identity, parent, error=False):
        return message(identity, parent, {'role':'toolResult','toolCallId':'reused-call',
            'toolName':'write','isError':error,'content':[{'type':'text','text':'Original result'}],
            'details':details})
    rows = [header,user,request('request1','user'),result('result1','request1'),
        {'type':'custom', 'id':'extension','parentId':'result1','data':'Opaque extension'},
        request('request2','extension'),result('result2','request2'),
        request('failed-request','result2'),result('failed-result','failed-request',True),
        request('foreign-request','user'),result('foreign-result','foreign-request')]
    saved.write_text(''.join(json.dumps(row)+'\n' for row in rows))
    saved.chmod(0o600)
    original_native = saved.read_bytes()
    witness = NativeWitness(header['id'],str(saved),'failed-result','user',
        FileRevision.from_stat(saved.stat()))
    assert witness.revision == SessionRevision.observe(str(saved)).require_available().native
    owner = admit(comms, 'artifact-owner')
    comms.registry.register(replace(owner,session_file=str(saved)))
    owner = comms.registry.require(owner.name)
    _, generation = comms.registry.live_owner_with_generation(owner.name)

    class EmptyFutureQueue(FutureInputQueue):
        def future_inputs(self, owner, pending_input_key):
            return {}

    boundary = CompactionBoundary(comms.registry,
        InputDispositions(comms.root/InputDispositions.filename), EmptyFutureQueue())
    with boundary.hold(owner,generation,witness) as held:
        source = held.capture((), None)
        source.require_current(held)
    facts = tuple(fact for fact in source.retained.facts if isinstance(fact,NativeArtifactTaskFact))
    assert tuple(fact.source.entries for fact in facts) == (
        ('request1','result1'),('request2','result2'))
    assert all(fact.source.path == str(saved) for fact in facts)
    assert all(fact.artifact.digest == TextDigest.of(text) for fact in facts)
    assert FieldCodec.decode(RetainedTaskFacts,FieldCodec.encode(source.retained)) == source.retained
    source.retained.require_summary(source.retained.compaction_text+'\n\nNarrative')
    assert saved.read_bytes() == original_native
    # The same metadata without an original call cannot fabricate a proved pair.
    orphan = NativeEntry.from_evidence(result('orphan','user'))
    with pytest.raises(ValueError,match='original SDK request'):
        orphan.retained_tool_facts(source.native, {})
    saved.write_bytes(original_native+b'\n')
    with pytest.raises(ValueError,match='changed since preparation'):
        witness.retained_task_facts()
