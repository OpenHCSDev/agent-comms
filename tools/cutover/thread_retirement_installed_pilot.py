"""One installed provider-free original000 -> target C3 retained batch journey."""
import asyncio
from contextlib import closing
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import subprocess
import shutil
import sqlite3
import sys

from agent_comms.active_route import read_active_route
from agent_comms.bus_publication import stable_thread_lookup
from agent_comms.child_process import ObservedProcess, ProcessIdentity
from agent_comms.comms import Comms
from agent_comms.compaction_records import PrivateRawInput
from agent_comms.coordination_cohort import next_sealed_assignment
from agent_comms.coordinator import Coordination
from agent_comms.field_codec import FieldCodec
from agent_comms.owner_launch import RetainedOwnerLaunch
from agent_comms.owner_lifecycle import OwnerReleaseReceipt
from agent_comms.input_disposition import InputDispositions
from agent_comms.maintenance_barrier import PausedPhase
from agent_comms.registry_document import RegistryDocument
from agent_comms.private_bus_checkpoint import install_private_bus_checkpoint
from agent_comms.store_files import _store_lock
from agent_comms.threads import Thread
from agent_comms.wire_log import WireLog
from seed_thread_retirement_fixture import historical_reservation, ready

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'tests'))
from maintenance_control_fixture import FixtureMaintenanceControl


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def prompt_count(root):
    # Every selected raw prompt writer reserves this original marker before
    # touching stdin. Count original admission records, not observed absence of
    # a process or a UI event. The saved native journal must also remain absent.
    with closing(sqlite3.connect((root/'compaction-commits.sqlite3').as_uri()+'?mode=ro', uri=True)) as db:
        db.execute('PRAGMA query_only=ON')
        count = len(PrivateRawInput.select(db))
    assert not list((root/'native-sessions').rglob('*.jsonl'))
    return count


def old_client_write_case(stage, root, original_python, environment):
    """Quarantine the destructive negative after all fixture workers retire."""
    copied = stage/'old-client-wire'
    shutil.copytree(root, copied)
    RegistryDocument.from_wire(json.loads((copied/'registry.json').read_text()))
    # A copied bus has a new inode. Author only this disposable copy's derived
    # certificate through the existing installer; preserve all original wire,
    # registry, native, reservations and the accepted run's certificate bytes.
    with _store_lock(copied/'wire'):
        log = WireLog(copied/'bus.jsonl')
        marker = log.read_metadata_unlocked()
        log.write_metadata_unlocked(replace(marker, checkpoint_version=None, checkpoint_seal=None))
        (copied/'private_bus_checkpoint.sqlite3').unlink()
        install_private_bus_checkpoint(log)
    control = FixtureMaintenanceControl(Comms(copied).owners.maintenance)
    control.advance(control.begin('disposable-old-client-probe'), PausedPhase)
    result = subprocess.run([original_python, str(Path(__file__).with_name('old_thread_client_probe.py')),
                             str(copied)], env=environment, capture_output=True, text=True, timeout=8)
    (stage/'old-client-stderr.txt').write_text(result.stderr)
    assert result.returncode == 0, result.stderr
    evidence = json.loads(result.stdout)
    try:
        RegistryDocument.from_wire(json.loads((copied/'registry.json').read_text()))
    except ValueError as error:
        assert "Unknown fields for Thread: ['last_goal_report_turn']" in str(error)
        evidence['target_strict_reader_refuses'] = str(error)
    else:
        raise AssertionError('Target accepted the old client reintroduced member')
    assert prompt_count(copied) == 0
    evidence['public_activation_requires_old_client_retirement'] = True
    (stage/'old-client-sanitized-receipt.json').write_text(json.dumps(evidence, indent=2)+'\n')
    return evidence


def main():
    stage = Path(os.environ['AC_PHASE_FIXTURE_STAGE'])
    stage.mkdir(mode=0o700, parents=True, exist_ok=False)
    root, route, receipt = stage/'wire', stage/'route'/'active-route.json', stage/'receipt.json'
    original_python = os.environ['AC_PHASE_ORIGINAL_PYTHON']
    environment = dict(os.environ)
    environment.pop('PYTHONPATH', None)
    environment['AC_NATIVE_COPIED_PACKAGE'] = os.environ['AC_PHASE_ORIGINAL_PACKAGE']
    seed = subprocess.Popen([original_python, str(Path(__file__).with_name('seed_thread_retirement_fixture.py')),
                             str(root), str(route)], env=environment, stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=(stage/'seed-stderr.txt').open('w'), text=True)
    originals, replacements = [], []
    try:
        line = seed.stdout.readline()
        assert line, (stage/'seed-stderr.txt').read_text()
        source = json.loads(line)
        originals = [FieldCodec.decode(ProcessIdentity, item) for item in source['owners']]
        expected = [FieldCodec.decode(Thread, item) for item in source['original_threads']]
        expected[1] = replace(expected[1], active_turn=None)
        protected = [root/'bus.jsonl', root/'protected-native.jsonl', root/InputDispositions.filename,
                     root/'diagnostics'/f"{source['historical_input_id']}.json"]
        hashes = {str(path.relative_to(root)): digest(path) for path in protected}
        assert prompt_count(root) == 0
        before_registry, before_route = digest(root/'registry.json'), digest(route)
        command = [sys.executable, str(Path(__file__).with_name('restart_thread_format.py')),
                   '--original-python', original_python, '--root', str(root), '--root-id', source['root_id'],
                   '--native-package', os.environ['AC_NATIVE_COPIED_PACKAGE'], '--route-path', str(route),
                   '--receipt', str(receipt)]
        refused = subprocess.run(command, capture_output=True, text=True)
        (stage/'busy-refusal.txt').write_text(refused.stderr)
        assert refused.returncode != 0
        assert digest(root/'registry.json') == before_registry and digest(route) == before_route
        assert all(ObservedProcess(owner).alive() for owner in originals)
        assert not receipt.exists() and not receipt.with_name(receipt.name+'.originals').exists()
        seed.stdin.write('idle\n')
        seed.stdin.flush()
        assert seed.stdout.readline().strip() == 'idle'
        installed = subprocess.run(command, capture_output=True, text=True)
        (stage/'cutover-stderr.txt').write_text(installed.stderr)
        (stage/'cutover-stdout.txt').write_text(installed.stdout)
        assert installed.returncode == 0, installed.stderr
        results = json.loads(installed.stdout)
        assert len(results) == 2 and all(not ObservedProcess(owner).alive() for owner in originals)
        service = Comms(root)
        replacements = [service.registry.require(item['thread']).process_identity for item in results]
        for index, (name, args, credential) in enumerate((
            ('phase-alpha', ('--offline','--no-tools','--thinking','off'), 'fixture-alpha'),
            ('phase-renamed', (), 'fixture-beta'),
        )):
            owner = service.registry.require(name)
            asyncio.run(ready(root, owner))
            launch = RetainedOwnerLaunch.capture(owner, service.registry.snapshot())
            assert launch.arguments == args and launch.environment['BATCH_OWNER_CREDENTIAL'] == credential
            assert launch.environment['AGENT_COMMS_THREAD'] == name
            assert owner == replace(expected[index], process_identity=owner.process_identity)
            assert not Path(f'/proc/{owner.pid}/task/{owner.pid}/children').read_text().strip()
        with Coordination(str(root/'coordination.sqlite3')) as store:
            owner = service.registry.require('phase-alpha')
            assert next_sealed_assignment(store, stable_thread_lookup(owner.created_at), owner.name,
                after_seq=0) is None, 'Historical failed reservation became a fresh native attempt'
        assert historical_reservation(root, source['historical_input_id']) == source['historical_reservation_sha256']
        assert all(row.public_status == 'unknown'
                   for row in InputDispositions(root/InputDispositions.filename).read().rows.values())
        assert prompt_count(root) == 0
        releases = FieldCodec.decode(dict[str, OwnerReleaseReceipt],
                                     json.loads((root/'owner_release_receipts.json').read_text()))
        assert releases['phase-retired'].thread.name == 'phase-retired'
        assert all('last_goal_report_turn' not in FieldCodec.encode(item.thread) for item in releases.values())
        assert hashes == {str(path.relative_to(root)): digest(path) for path in protected}
        assert read_active_route(route).native_package == Path(os.environ['AC_NATIVE_COPIED_PACKAGE'])
        proof = json.loads(receipt.read_text())
        assert proof['target_validation'] == {'threads': 4, 'releases': 2}
        assert proof['phase'] == 'target_owners_launched' and proof['route_published_before_first_launch']
        proof.update(busy_refusal_without_stops=True, retained_distinct_settings=True,
                     renamed_owner_retained=True, both_thread_carriers_retired=True,
                     original_native_wire_unknown_unchanged=True,
                     protected_hashes=hashes, actual_target_runtime_attachment=True,
                     original_python=original_python, target_python=sys.executable,
                     source_head=os.environ['AC_PHASE_CORE_HEAD'], complete=True)
    finally:
        seed.stdin.close()
        seed.wait(timeout=5)
        # Only fixture-owned exact process identities; never resolve a public
        # owner or infer another format after a failed transition.
        for identity in (*replacements, *originals):
            process = ObservedProcess(identity)
            if process.alive():
                process.stop_sync()
    # Close both complete worker lifetimes before the final no-replay assertion:
    # late work cannot escape a check taken only at runtime attachment.
    assert prompt_count(root) == 0
    assert historical_reservation(root, source['historical_input_id']) == source['historical_reservation_sha256']
    assert hashes == {str(path.relative_to(root)): digest(path) for path in protected}
    assert all(service.registry.require(prior.name).goal == prior.goal for prior in expected)
    proof.update(historical_unknown_not_reclassified=True, historical_failure_receipt_unchanged=True,
                 historical_reservation_sha256=source['historical_reservation_sha256'],
                 historical_assignment_ineligible=True, blocked_goal_not_resumed=True,
                 prompt_count=0, fixture_processes_retired=True)
    proof['old_client_format_negative'] = old_client_write_case(stage, root, original_python, environment)
    (stage/'sanitized-receipt.json').write_text(json.dumps(proof, indent=2)+'\n')
    print(json.dumps(proof), flush=True)


if __name__ == '__main__':
    main()
