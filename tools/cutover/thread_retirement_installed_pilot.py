"""One installed provider-free original000 -> target C3 retained batch journey."""
import asyncio
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

from agent_comms.active_route import read_active_route
from agent_comms.child_process import ObservedProcess, ProcessIdentity
from agent_comms.comms import Comms
from agent_comms.field_codec import FieldCodec
from agent_comms.owner_launch import RetainedOwnerLaunch
from agent_comms.owner_lifecycle import OwnerReleaseReceipt
from agent_comms.input_disposition import InputDispositions
from agent_comms.threads import Thread
from seed_thread_retirement_fixture import ready


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


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
        protected = [root/'bus.jsonl', root/'protected-native.jsonl', root/InputDispositions.filename]
        hashes = {str(path.relative_to(root)): digest(path) for path in protected}
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
        releases = FieldCodec.decode(dict[str, OwnerReleaseReceipt],
                                     json.loads((root/'owner_release_receipts.json').read_text()))
        assert releases['phase-retired'].thread.name == 'phase-retired'
        assert all('last_goal_report_turn' not in FieldCodec.encode(item.thread) for item in releases.values())
        assert hashes == {str(path.relative_to(root)): digest(path) for path in protected}
        assert read_active_route(route).native_package == Path(os.environ['AC_NATIVE_COPIED_PACKAGE'])
        proof = json.loads(receipt.read_text())
        assert proof['target_validation'] == {'threads': 3, 'releases': 1}
        assert proof['phase'] == 'target_owners_launched' and proof['route_published_before_first_launch']
        proof.update(busy_refusal_without_stops=True, retained_distinct_settings=True,
                     renamed_owner_retained=True, both_thread_carriers_retired=True,
                     original_native_wire_unknown_unchanged=True,
                     protected_hashes=hashes, actual_target_runtime_attachment=True,
                     original_python=original_python, target_python=sys.executable,
                     source_head=os.environ['AC_PHASE_CORE_HEAD'], complete=True)
        (stage/'sanitized-receipt.json').write_text(json.dumps(proof, indent=2)+'\n')
        print(json.dumps(proof), flush=True)
    finally:
        seed.stdin.close()
        seed.wait(timeout=5)
        # Only fixture-owned exact process identities; never resolve a public
        # owner or infer another format after a failed transition.
        for identity in (*replacements, *originals):
            process = ObservedProcess(identity)
            if process.alive():
                process.stop_sync()


if __name__ == '__main__':
    main()
