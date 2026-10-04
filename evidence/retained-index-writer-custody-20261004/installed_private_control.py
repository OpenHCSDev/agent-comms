"""One original private writer/resource batch; no old schema is synthesized."""
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

from agent_comms.child_process import Platform, ProcessIdentity
from agent_comms.field_codec import FieldCodec
from agent_comms.threads import Thread
from agent_comms.transcript_receipts import AssignedTranscriptSource
from agent_comms.wire_log import WireLog


def main():
    tools, output = map(Path, sys.argv[1:])
    output.mkdir(parents=True, exist_ok=False)
    origin = Path(__import__('agent_comms').__file__).resolve()
    assert origin.is_relative_to(Path(sys.executable).parent.parent)
    environment = dict(os.environ)
    environment.pop('PYTHONPATH', None)
    children, results = [], {}
    began = time.monotonic()

    def digest(path):
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def files(root, excluded=()):
        return {str(path.relative_to(root)): digest(path)
                for path in sorted(root.rglob('*'))
                if path.is_file() and path.name not in excluded}

    def run(label, script, *arguments, descriptors=(), refused=None):
        with subprocess.Popen(
            [sys.executable, str(tools / script), *map(str, arguments)],
            env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            pass_fds=descriptors, start_new_session=True,
        ) as process:
            identity = ProcessIdentity.capture(process.pid)
            children.append(identity)
            stdout, stderr = process.communicate()
            (output / (label + '.stdout')).write_bytes(stdout)
            (output / (label + '.stderr')).write_bytes(stderr)
            results[label] = {'identity': FieldCodec.encode(identity),
                              'returncode': process.returncode}
            assert not identity.alive()
            if refused is not None:
                assert process.returncode != 0 and refused in stderr.decode(), stderr.decode()
                return None
            assert process.returncode == 0, stderr.decode()
            return json.loads(stdout)

    root = output / 'wire'
    receipt = {'state': 'FAILED', 'python': sys.executable,
               'application_origin': str(origin), 'root': str(root),
               'controller': FieldCodec.encode(ProcessIdentity.capture(os.getpid())),
               'cross_version_writer_restart_qualified': False}
    try:
        seed = run('seed', 'seed_retained_index_fixture.py', root)
        original = files(root)
        bus = WireLog(root / 'bus.jsonl')
        root_id = seed['root_id']
        installer = tools / 'install_retained_index.py'
        run('same-schema-refusal', 'retained_index_writer.py', root,
            sys.executable, installer, root_id,
            refused='Target schema no longer requires retained reset.')
        assert files(root) == original
        run('original-root-refusal', 'retained_index_writer.py', root,
            sys.executable, installer, root_id + '-wrong',
            refused='Original private writer proof changed.')
        assert files(root) == original

        excluded = ('private_bus_checkpoint.sqlite3', 'bus_meta.json')
        protected = files(root, excluded)
        index = root / 'private_bus_checkpoint.sqlite3'
        with bus.locked() as custody:
            marker = bus.read_metadata_unlocked()
            descriptor_identity = os.fstat(custody.descriptor)
            lock_identity = (root / '.bus.jsonl.lock').stat()
            assert (descriptor_identity.st_dev, descriptor_identity.st_ino) == (
                lock_identity.st_dev, lock_identity.st_ino)
            with open(os.devnull, 'rb') as unrelated:
                run('descriptor-refusal', 'install_retained_index.py', root,
                    unrelated.fileno(), root_id, descriptors=(unrelated.fileno(),),
                    refused='Inherited descriptor is not the original bus writer lock.')
            run('installer-root-refusal', 'install_retained_index.py', root,
                custody.descriptor, root_id + '-wrong', descriptors=(custody.descriptor,),
                refused='Original root identity changed before installation.')
            assert files(root) == original
            preimage = output / 'derived-index-preimage'
            preimage.mkdir()
            for name in excluded:
                (preimage / name).write_bytes((root / name).read_bytes())
            retained = replace(marker, checkpoint_version=None, checkpoint_seal=None)
            bus.write_metadata_unlocked(retained)
            index.unlink()
            installed = run('inherited-rebuild', 'install_retained_index.py', root,
                            custody.descriptor, root_id, descriptors=(custody.descriptor,))
            assert installed['retained_writer_descriptor_verified'] is True
            assert installed['root_id'] == root_id and installed['through_seq'] == marker.last_seq
            after = bus.read_metadata_unlocked()
            assert replace(after, checkpoint_version=None, checkpoint_seal=None) == retained
        assert files(root, excluded) == protected

        # Original pointer/frozen audience survives a real target index install.
        sender = FieldCodec.decode(Thread, seed['sender'])
        rows = AssignedTranscriptSource.for_thread(root, sender, bus).rows(limit=10)
        assert len(rows) == 1 and rows[0].message.to_wire() == seed['original']
        assert sorted(row.canonical_thread for row in rows[0].audience.recipients) == ['alpha', 'beta']
        rebuilt = files(root)
        seal_root = output / 'missing-seal-wire'
        seal_seed = run('seal-seed', 'seed_retained_index_fixture.py', seal_root)
        seal_log = WireLog(seal_root / 'bus.jsonl')
        seal_original = files(seal_root)
        with seal_log.locked():
            seal_marker = seal_log.read_metadata_unlocked()
            (output / 'seal-marker-preimage.json').write_bytes(seal_log.metadata_path.read_bytes())
            seal_log.write_metadata_unlocked(replace(
                seal_marker, checkpoint_version=None, checkpoint_seal=None))
        unsealed = files(seal_root)
        run('seal-refusal', 'retained_index_writer.py', seal_root,
            sys.executable, installer, seal_seed['root_id'],
            refused='Private checkpoint lacks durable marker binding.')
        assert files(seal_root) == unsealed
        assert {name: value for name, value in unsealed.items() if name != 'bus_meta.json'} == {
            name: value for name, value in seal_original.items() if name != 'bus_meta.json'}
        # Damage only this NEW disposable index. Never fabricate an old producer
        # schema or modify the marker to grant the damaged schema a certificate.
        with sqlite3.connect(index) as connection:
            connection.execute('CREATE TABLE unexpected_sidecar_member(value TEXT)')
        damaged = files(root)
        run('wrong-schema-refusal', 'retained_index_writer.py', root,
            sys.executable, installer, root_id,
            refused='Private bus checkpoint schema is unavailable.')
        assert files(root) == damaged
        assert files(root, ('private_bus_checkpoint.sqlite3',)) == {
            name: value for name, value in rebuilt.items()
            if name != 'private_bus_checkpoint.sqlite3'}
        receipt.update(state='PASS', results=results, original_files=original,
                       retained_files=protected, rebuilt_files=rebuilt,
                       rejected_derived_files=damaged, installed=installed,
                       missing_seal_files=unsealed,
                       frozen_original_and_audience_unchanged=True,
                       original_marker_identity_and_admission_unchanged=True,
                       original_wire_registry_settings_unchanged=True,
                       native_inputs=0, provider_calls=0, public_operations=0,
                       original_input_replays=0)
    finally:
        receipt['elapsed_seconds'] = time.monotonic() - began
        receipt['children'] = [FieldCodec.encode(identity) for identity in children]
        receipt['alive_children'] = [FieldCodec.encode(identity) for identity in children if identity.alive()]
        receipt['groups'] = [FieldCodec.encode(member) for identity in children
                             for member in Platform.current().group_members(identity)]
        receipt['sockets'] = [str(path) for path in output.rglob('*') if path.is_socket()]
        (output / 'receipt.json').write_text(json.dumps(receipt, indent=2) + '\n')
    assert not receipt['alive_children'] and not receipt['groups'] and not receipt['sockets']
    print(json.dumps({'state': receipt['state'], 'elapsed_seconds': receipt['elapsed_seconds'],
                      'receipt': str(output / 'receipt.json')}), flush=True)


if __name__ == '__main__':
    main()
