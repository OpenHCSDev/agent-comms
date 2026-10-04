"""Continuous original publisher -> stopped carry -> installed USER source."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

from agent_comms.comms import Comms
from agent_comms.retained_task_facts import RetainedTaskFacts
from retained_task_source_carry import RetainedTaskSourceCarry


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    stage, original_python = Path(sys.argv[1]), Path(sys.argv[2])
    stage.mkdir(mode=0o700, parents=True, exist_ok=False)
    root = stage / 'wire'
    environment = dict(os.environ)
    environment.pop('PYTHONPATH', None)
    seeded = subprocess.run([
        original_python, Path(__file__).with_name('seed_original_task_carry.py'), root,
    ], env=environment, capture_output=True, text=True)
    (stage / 'seed.log').write_text(seeded.stdout + seeded.stderr)
    seeded.check_returncode()
    seed = json.loads(seeded.stdout)
    service = Comms(root)
    protected = {path.name: digest(path) for path in
                 (root / 'registry.json', root / 'protected-fixture-source.proof')}
    original_bus = (root / 'bus.jsonl').read_bytes()
    operation = RetainedTaskSourceCarry(original_python, root, seed['root_id'], stage / 'carry.json')
    if '--reject-corrupt-original' in sys.argv:
        # Original certificate still binds the real original publisher bytes.
        # A late-row mutation must refuse before fences, backups or publication.
        altered = original_bus.replace(b'ORIGINAL', b'CORRUPT!', 1)
        assert altered != original_bus
        (root / 'bus.jsonl').write_bytes(altered)
        before = {path.name: digest(path) for path in root.iterdir() if path.is_file()}
        try:
            service.owners.restart_owners(cutover=operation)
        except subprocess.CalledProcessError:
            pass
        else:
            raise AssertionError('Corrupt original certificate was admitted.')
        assert all(digest(root / name) == expected for name, expected in before.items())
        assert not operation.receipt.exists()
        assert not operation.receipt.with_name(operation.receipt.name + '.originals').exists()
        receipt = {'state': 'CORRUPT_ORIGINAL_REFUSED_BEFORE_CARRY',
                   'original_files_unchanged': True, 'preimages_created': False,
                   'provider_calls': 0, 'input_replays': 0, 'public_changes': []}
        (stage / 'receipt.json').write_text(json.dumps(receipt, indent=2) + '\n')
        print(json.dumps(receipt), flush=True)
        return
    # Exercise the actual central operation even with zero live native owners.
    # The publisher cleared its owned fixture process before returning above.
    assert service.owners.restart_owners(cutover=operation) == ()
    assert all(digest(root / name) == expected for name, expected in protected.items())
    originals = stage / 'carry.json.originals'
    assert (originals / 'bus.jsonl').read_bytes() == original_bus
    with service.bus.log.certified_read() as source:
        records = tuple(service.bus.log.verified_records_unlocked(source.marker))
        original = records[0].message
        assert original.task.declared_name == 'choice'
        assert 'decision' not in original.to_wire()
    beta = service.registry.require('beta')
    subject = service.messaging.send_user_message(
        beta.name, 'USER original μ\nPreserve my exact multiline constraint.', worktree=beta.worktree)
    pin = service.messaging.pin_user_constraint(beta.name, subject.reference, worktree=beta.worktree)
    with service.bus.log.certified_read() as source:
        facts = tuple(source.retained_task_facts(beta.incarnation))
    retained = RetainedTaskFacts(facts).for_owner(beta, service.registry.snapshot())
    # The original model turn ended. Its unchanged scope is historical, not
    # promoted to a new accepted model lease by this carry.
    assert any(fact.source == original for fact in retained.facts)
    assert original not in retained.current_authored_sources(beta, service.registry.snapshot())
    assert pin in retained.current_authored_sources(beta, service.registry.snapshot())
    assert retained.original_text_source(pin) == subject
    # Reopening both source and registry must derive the same original lineage.
    reopened = Comms(root)
    assert reopened.bus.log.full_history()[-1] == pin
    before = (root / 'bus.jsonl').read_bytes()
    try:
        reopened.owners.restart_owners(cutover=operation)
    except ValueError:
        pass
    else:
        raise AssertionError('A completed carry was applied twice.')
    assert (root / 'bus.jsonl').read_bytes() == before
    receipt = {'state': 'INSTALLED_ORIGINAL_TASK_CARRY_USER_SOURCE_PASS',
               'target_python': sys.executable, 'original_python': str(original_python),
               'original_reference': seed['original'], 'user_subject': subject.to_wire(),
               'carry': json.loads((stage / 'carry.json').read_text()),
               'registry_and_opaque_fixture_bytes_unchanged': True,
               'original_bus_preimage_preserved': True, 'repeat_refused_before_mutation': True,
               'provider_calls': 0, 'native_launches': 0, 'input_replays': 0,
               'full_native_ACP_UI_acceptance': False, 'public_changes': []}
    (stage / 'receipt.json').write_text(json.dumps(receipt, indent=2) + '\n')
    print(json.dumps(receipt), flush=True)


if __name__ == '__main__':
    main()
