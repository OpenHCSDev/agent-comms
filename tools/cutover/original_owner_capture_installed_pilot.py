"""Provider-free genuine original admission, strict target capture and refusal."""
import hashlib
import json
import os
from pathlib import Path
import subprocess

from agent_comms.field_codec import FieldCodec
from agent_comms.owner_lifecycle import OwnerReleaseReceipt
from agent_comms.registry_document import RegistryDocument
from original_owner_capture import OriginalTypedCapture
from thread_format_retirement import GoalReportMemberRetirement


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    stage = Path(os.environ['AC_CAPTURE_FIXTURE_STAGE'])
    assert stage.is_relative_to('/home/ts/wt')
    stage.mkdir(mode=0o700, parents=True)
    root = stage / 'original'
    original_python = Path(os.environ['AC_CAPTURE_ORIGINAL_PYTHON'])
    environment = dict(os.environ)
    environment.pop('PYTHONPATH', None)
    environment.update(AGENT_COMMS_THREAD='captured-original',
                       AGENT_COMMS_AGENT_BIN=str(original_python.parent / 'pi-comms-native'),
                       AGENT_COMMS_AGENT_ARGS='--fixture-private "original settings"',
                       ORIGINAL_CAPTURE_CREDENTIAL='RAM-only-private-fixture')
    process = subprocess.Popen([
        str(original_python), str(Path(__file__).with_name('seed_original_owner_capture.py')),
        str(root),
    ], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, env=environment)
    try:
        ready = process.stdout.readline()
        assert ready, process.stderr.read()
        assert json.loads(ready)['pid'] == process.pid
        original_registry = digest(root / 'registry.json')
        original_releases = digest(root / 'owner_release_receipts.json')
        reader = OriginalTypedCapture(root, original_python)
        captured = reader.read('captured-original')
        assert captured.retained.process.pid == process.pid
        assert captured.retained.arguments == ('--fixture-private', 'original settings')
        assert captured.retained.environment['ORIGINAL_CAPTURE_CREDENTIAL'] == 'RAM-only-private-fixture'
        assert captured.require_current().incarnation == captured.source.incarnation
        assert original_registry == digest(root / 'registry.json')
        assert original_releases == digest(root / 'owner_release_receipts.json')
        document = json.loads((root / 'registry.json').read_text())
        target = RegistryDocument.from_wire(GoalReportMemberRetirement.threads(document))
        releases = FieldCodec.decode(dict[str, OwnerReleaseReceipt],
            GoalReportMemberRetirement.releases(json.loads(
                (root / 'owner_release_receipts.json').read_text())))
        assert target.threads[captured.source.name] == captured.source
        assert releases['retired-original'].before == 1
        assert releases['retired-original'].after == 2
        process.stdin.write('advance-admission\n')
        process.stdin.flush()
        assert process.stdout.readline().strip() == 'advanced'
        try:
            captured.require_current()
        except subprocess.CalledProcessError:
            refused = True
        else:
            raise AssertionError('A changed admission reused original capture proof')
        receipt = {
            'complete': True, 'authentic_original_python': str(original_python),
            'original_registry_read_only': True, 'original_releases_read_only': True,
            'strict_target_registry': True, 'strict_target_release': True,
            'preserved_release_before_after': [1, 2],
            'distinct_original_arguments_retained': True, 'credentials_RAM_only': True,
            'changed_admission_refused': refused, 'provider_calls': 0,
            'native_inputs': 0, 'public_mutations': 0,
        }
        (stage / 'receipt.json').write_text(json.dumps(receipt, indent=2) + '\n')
        print(json.dumps(receipt), flush=True)
    finally:
        process.stdin.close()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.terminate()
            process.wait(timeout=5)
    assert process.returncode == 0, process.stderr.read()


if __name__ == '__main__':
    main()
