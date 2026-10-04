"""One authorized private-controller retirement; never an ACP success receipt."""
from dataclasses import fields, replace
import hashlib
import json
from pathlib import Path
import shutil
import time

from agent_comms.child_process import Platform, ProcessIdentity
from agent_comms.comms import Comms
from agent_comms.field_codec import FieldCodec
from agent_comms.input_attempt import StartedInput
from agent_comms.input_disposition import InputDispositions
from agent_comms.native_entries import NativeEvidenceRead, NativeEntry
from agent_comms.native_pi import NativeContextProof, NativeContextRecord

here = Path(__file__).resolve().parent
base = Path('/home/ts/wt/comms-async-bus-read-publication-custody-20261002/.artifacts/s4-three-cuts-configured01')
root = base / 'wire'
receipt_path = here / 'original560-controller-retirement.json'
assert not receipt_path.exists(), 'A recorded action must be inspected, never replayed'
teardown = json.loads((here / 'original560-private-teardown.json').read_text())
assert root == Path(teardown['private_root'])
assert teardown['both_groups_absent'] and teardown['observer_absent']
protected = teardown['protected_after']
digest = lambda path: hashlib.sha256(Path(path).read_bytes()).hexdigest()
assert all(digest(path) == expected for path, expected in protected.items())
identities = tuple(ProcessIdentity(pid, birth) for pid, birth in (
    (1583616, 46995415), (1589954, 47015057), (1589919, 47014847)))
platform = Platform.current()
assert all(not identity.alive() for identity in identities)
assert not platform.group_members(identities[0])
assert not platform.group_members(identities[1])
comms = Comms(root, private_initial_writes=False, private_claim_writes=False)
before = comms.registry.store.read()
thread = before.threads['source529']
assert thread.process_identity == identities[0]
lease = thread.require_turn_lease()
assert lease.turn_id == 'b4aa28174de24019ba3448e4d8e8f01f'
assert lease.admission_generation == before.admissions.generations[thread.name] == 5
assert thread.turn_generation == lease.identity.generation == 4
input_store = InputDispositions(root / InputDispositions.filename)
inputs = input_store.read()
row = inputs.rows['acp:fc5e8578176d46ec8b41e044de3ac974']
assert isinstance(row, StartedInput)
assert row.owner == thread.name and row.admission == lease.admission_generation
assert row.turn_id == lease.turn_id and row.native_id == '6c13198ae5958304e799129f371f44b7'
session = Path(thread.require_saved_session())
assert session.parent == base / 'forks'
with NativeEvidenceRead.open(session) as evidence:
    header, entries = evidence.observe()
    final = next(entry for entry in entries if entry.id == 'b7807f3e')
    assert final.assistant_message and final.final_reply
    branch = evidence.branch(final.id, entries)
    user = next(entry for entry in reversed(branch) if entry.input_boundary)
    assert user.input_id == row.native_id
    # Native inputDigest owns the serialized request envelope; StoredInput's
    # sent_digest owns UTF-8 text. Compare the original text through its owner.
    assert row.matches_native(turn_id=lease.turn_id, native_id=user.input_id,
                              text=user.message.text)
    proof = NativeContextProof.read_evidence(session, row.native_id, evidence=evidence)
    assert proof.session_id == header.id
    assert proof.session_entry_id == user.id
preimages = here.parents[1] / '.artifacts/original560-controller-retirement'
preimages.mkdir(mode=0o700, exist_ok=True)
activity_path = comms.agents.activity._path
checkpoint_path = comms.agents.activity.checkpoint.path
for path in (comms.registry.store.path, root / '.registry-owner-guard',
             activity_path, checkpoint_path):
    target = preimages / path.name
    if target.exists():
        assert digest(target) == digest(path)
    else:
        shutil.copy2(path, target)
record = NativeContextRecord(**{field.name: getattr(proof, field.name)
                               for field in fields(NativeContextRecord)})
receipt = {
    'authority': 'Explicit OPTION2 original private dead-controller lifecycle retirement',
    'root': str(root), 'thread': thread.name,
    'original_lease': FieldCodec.encode(lease),
    'native_final': final.id, 'native_context_proof': FieldCodec.encode(record),
    'native_session_file': str(proof.session_file),
    'original_started_key': row.key,
    'processes_absent': [FieldCodec.encode(identity) for identity in identities],
    'both_groups_absent': True, 'observer_absent': True,
    'preimages': str(preimages), 'protected_before': protected,
    'acp_disposition': 'ACP_UNCONFIRMED', 'input_resolved': False,
    'provider_calls': 0, 'replayed_inputs': 0, 'republished_messages': 0,
    'action_attempted_at_ms': int(time.time() * 1000),
}
receipt_path.write_text(json.dumps(receipt, indent=2) + '\n')
# Exact CAS belongs to the existing activity/registration/document owner.
# No outer BUS descriptor is retained across this call.
fence = comms.agents.finish_turn(lease)
after = comms.registry.store.read()
successor = after.threads[thread.name]
assert fence is not None and fence.matches(lease)
assert successor.active_turn is None and successor.last_finished_turn_id == lease.turn_id
assert successor == replace(thread, active_turn=None, last_finished_turn_id=lease.turn_id)
assert after == replace(before, threads={**before.threads, thread.name: successor},
                        last_seen={**before.last_seen, thread.name: after.last_seen[thread.name]})
changed = [path for path, expected in protected.items() if digest(path) != expected]
allowed = {str(comms.registry.store.path), str(activity_path),
           str(checkpoint_path), str(root / '.registry-owner-guard')}
assert set(changed) <= allowed
assert input_store.read() == inputs
assert all(not identity.alive() for identity in identities)
assert not platform.group_members(identities[0]) and not platform.group_members(identities[1])
receipt.update({
    'cas_result': FieldCodec.encode(fence), 'lifecycle_retired': True,
    'registry_changed_fields': ['threads.source529.active_turn',
        'threads.source529.last_finished_turn_id', 'last_seen.source529'],
    'changed_paths': changed,
    'protected_after': {path: digest(path) for path in protected},
    'started_input_unchanged': True, 'all_other_registry_fields_unchanged': True,
    'completed_at_ms': int(time.time() * 1000),
})
receipt_path.write_text(json.dumps(receipt, indent=2) + '\n')
print(json.dumps({key: receipt[key] for key in (
    'root', 'thread', 'cas_result', 'lifecycle_retired', 'registry_changed_fields',
    'changed_paths', 'started_input_unchanged', 'acp_disposition', 'input_resolved')}, indent=2))
