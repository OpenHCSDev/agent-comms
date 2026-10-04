"""One-use recovery cohort publication at the ORIGINAL stopped-owner seam.

Parent alone executes. Full audience/settings/process proof is acquired in RAM;
no input replay, settings restoration, runtime-input reset or owner start loop.
No actual-gate hashes are supplied by this source. --execute requires reviewed
external UI and archived-source gate artifacts, separate from package metadata.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, replace
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from agent_comms.active_route import ActiveRoute, active_route_path, read_active_route, _publish_active_route_locked
from agent_comms.child_process import ProcessIdentity, Platform
from agent_comms.comms import Comms
from agent_comms.field_codec import FieldCodec
from agent_comms.historical_views import HistoryArchive, HistorySource
from agent_comms.input_disposition import InputDispositions
from agent_comms.native_package import verify_native_package
from agent_comms.owner_cutover import StoppedOwnerInstallation
from agent_comms.owner_restart import OwnerRestartRequest
from agent_comms.owner_launch import RestartEnvironment, RetainedOwnerLaunch
from agent_comms.owner_lifecycle import OwnerRestartSelection
from agent_comms.private_path import FileRevision, PrivateFileRole
from agent_comms.store_files import _atomic_write_text
from carry_history_provenance import carry

ROOT = Path('/var/tmp/agent-comms-live-20260927-wzjtqhza')
ROOT_ID = 'e206f3766e60451a989ca34df0e2a94b'
TARGET = Path('/home/ts/wt/comms-cleanup-live-integration-20260929/.artifacts/runtime-openhcs-recovery-cohort-20261001')
NATIVE = Path('/home/ts/.local/share/agent-comms/native-current-593b978a717ae8f6/node_modules/@earendil-works/pi-coding-agent')
SOURCE_PROOF = TARGET.parent / 'staging-openhcs-recovery-cohort-20261001/source-proof.json'
PINS = {'agent_comms': 'cac7bdf337d9e4dba888ce7c2093ddcc55802bc3',
        'toad': '7572b7b7856b1ac99f7bda5dac760c87c586a73d',
        'textual': '6b5895fa0a72aeec2aeaef7206d5debfa0c1803c'}
OLD_WRITER = Path('/home/ts/.local/share/agent-comms/runtime-canonical-native-checkpoint-20260929/bin/python')
WRITER = Path('/home/ts/wt/comms-cleanup-live-integration-20260929/tools/cutover/retained_index_writer.py')
INSTALLER = WRITER.with_name('install_retained_index.py')
SCHEMA = WRITER.with_name('checkpoint_schema.py')
ARCHIVES = {'source-0njqz198': 'b8ff29c8e11f47f1a59b568650e580a3',
            'source-a7v0vr4w': 'aaed93d6297f4c15bccc68c211066c7e'}
LINKS = Path('/home/ts/.local/bin')
COMMANDS = ('agent-comms', 'agent-comms-acp', 'agent-comms-agent', 'agent-comms-nk-foreground', 'toad')


def digest(path):
    with path.open('rb') as opened:
        return hashlib.file_digest(opened, 'sha256').hexdigest()


def clean_environment():
    environment = dict(os.environ)
    environment.pop('PYTHONPATH', None)
    return environment


def require_stage(args):
    if sys.executable != str(TARGET / 'bin/python'):
        raise RuntimeError('Use the exact reviewed target interpreter')
    for path, expected in ((TARGET / 'activation.json', args.activation_sha256),
                           (SOURCE_PROOF, args.source_proof_sha256),
                           (WRITER, args.writer_sha256), (INSTALLER, args.installer_sha256),
                           (SCHEMA, args.schema_script_sha256)):
        if digest(path) != expected:
            raise RuntimeError(f'Reviewed artifact changed: {path}')
    activation = json.loads((TARGET / 'activation.json').read_text())
    if activation['stage'] != str(TARGET) or activation['pins'] != PINS:
        raise RuntimeError('Wrong frozen source cohort')
    if activation['sdk'] != '0.12.1' or activation['native_package'] != str(NATIVE):
        raise RuntimeError('Wrong SDK/native pair')
    if activation['staging_receipt'] != str(SOURCE_PROOF):
        raise RuntimeError('Wrong source proof relation')
    proof = json.loads(SOURCE_PROOF.read_text())
    if {row['module']: row['head'] for row in proof['sources']} != PINS:
        raise RuntimeError('Source proof names another cohort')
    if not all(row['byte_equal'] for row in proof['sources']) or not proof['native_full_trust']:
        raise RuntimeError('Unverified source/native proof')
    verify_native_package(NATIVE)


def require_no_clients(audience):
    """Decode argv boundaries once; explicit unrelated private roots are excluded.

    No PID is signal authority here. Exact original workers are checked by the
    batch. A Toad/ACP/CLI without a distinct explicit root is not safe to exclude.
    Unknown Python processes explicitly bound to this root also refuse.
    """
    platform = Platform.current()
    for entry in Path('/proc').iterdir():
        if not entry.name.isdecimal() or int(entry.name) == os.getpid():
            continue
        try:
            if entry.stat().st_uid != os.geteuid():
                continue
            identity = ProcessIdentity.capture(int(entry.name))
            argv = tuple(os.fsdecode(item) for item in (entry / 'cmdline').read_bytes().split(b'\0') if item)
            if not argv:
                continue
            head = argv[:3]
            module = head[2] if len(head) == 3 and head[1] == '-m' else ''
            commands = {Path(arg).name for arg in head}
            client = bool(commands.intersection((*COMMANDS, 'toad-comms'))) or module in {'toad', 'agent_comms'} or module.startswith('agent_comms.')
            python = Path(head[0]).name.startswith('python')
            if not client and not python:
                continue
            environment = dict(item.split(b'=', 1) for item in (entry / 'environ').read_bytes().split(b'\0') if b'=' in item)
            selected_root = environment.get(b'AGENT_COMMS_ROOT')
            platform.require(identity)
            if selected_root is not None and os.fsdecode(selected_root) != str(ROOT):
                continue
            if module == 'agent_comms.worker' and identity in {item.process for item in audience}:
                continue  # Only these exact workers belong to the canonical retained batch.
            if client or selected_root == os.fsencode(ROOT):
                raise RuntimeError(f'Unretired or unclassified client: exact process {identity}')
        except ProcessLookupError:
            continue
        except FileNotFoundError:
            continue
        # Permission/decoding failures deliberately refuse rather than infer absence.



def require_publication_preimages(original, current):
    if read_active_route() != original:
        raise RuntimeError('Original route changed')
    for command in COMMANDS:
        if (LINKS / command).readlink() != current / 'bin' / command:
            raise RuntimeError('Original default changed; recapture/review required')
        if not (TARGET / 'bin' / command).is_file():
            raise RuntimeError('Target entrypoint missing')
        temporary = LINKS / (command + '.openhcs-recovery-publish')
        if temporary.exists() or temporary.is_symlink():
            raise RuntimeError('Previous publication temporary requires review')


def retain_file(path, destination):
    """Original private bytes/stat custody; no decoding or repairing preimages."""
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), 'rb') as opened:
        before = os.fstat(opened.fileno())
        PrivateFileRole().require(before)
        payload = opened.read()
        revision = FileRevision.from_stat(before)
        if revision != FileRevision.from_stat(os.fstat(opened.fileno())) or revision != FileRevision.from_stat(path.lstat()):
            raise RuntimeError(f'Original file changed under stopped custody: {path}')
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'wb') as output:
        os.fchown(output.fileno(), before.st_uid, before.st_gid)
        output.write(payload)
        output.flush()
        os.utime(output.fileno(), ns=(before.st_atime_ns, before.st_mtime_ns))
        os.fsync(output.fileno())
    return {'path': str(path), 'sha256': hashlib.sha256(payload).hexdigest(),
            'size': before.st_size, 'mode': before.st_mode, 'uid': before.st_uid,
            'gid': before.st_gid, 'revision': FieldCodec.encode(revision)}


def fsync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@dataclass(frozen=True)
class PublishOpenhcsRecovery(StoppedOwnerInstallation):
    audience: tuple[OwnerRestartSelection, ...]
    originals: tuple
    original_route: ActiveRoute
    route_directory: int
    current: Path
    receipt: Path
    manifest_bytes: bytes
    candidate: tuple[HistorySource, ...]
    carry_proof: dict

    def failed(self, failure):
        # This historical one-use member changes archived sources and has no
        # certified original-unchanged recovery. Leave its committed effects and
        # uncertainty intact; explicitly dispose before the one-shot exits.
        self.leave_stopped(failure)

    def complete(self, stopped):
        # FencedOwnerBatch retains the ORIGINAL wire custody through this
        # method. Readback belongs here, before resumed owners may progress.
        results = super().complete(stopped)
        after = stopped.lifecycle.registry.snapshot()
        for previous, result in zip(self.originals, results, strict=True):
            current = after.threads[result.thread]
            if result.thread != previous.name or replace(current, process_identity=previous.process_identity) != previous:
                raise RuntimeError('Original configuration readback differs under retained custody')
            RetainedOwnerLaunch.capture(current, after, interpreter=str(TARGET / 'bin/python'))
        self.note('retained-batch-launched-configurations-verified-public-ui-pending',
                  finished=time.time(), results=FieldCodec.encode(results))
        return results

    def bind_target_launch(self, lifecycle):
        lifecycle.pin_private_nk_launch(ROOT, ROOT_ID, NATIVE)

    @property
    def manifest(self):
        return ROOT / HistoryArchive.filename

    def note(self, phase, **facts):
        previous = json.loads(self.receipt.read_text())
        previous.update(phase=phase, **facts)
        _atomic_write_text(self.receipt, json.dumps(previous, indent=2)+'\n', fsync_parent=True)

    def require_sources(self):
        if self.manifest.read_bytes() != self.manifest_bytes:
            raise RuntimeError('Original history manifest changed; no old-reader inference')
        for source in self.candidate:
            source.validate()
        for proof in self.carry_proof['sources']:
            if any(digest(Path(proof['source']) / name) != expected
                   for name, expected in proof['original_hashes'].items()):
                raise RuntimeError('Original archive changed after carry preparation')

    def require_selection(self, snapshot, owners):
        if {thread.name for thread in owners} != {item.name for item in self.audience}:
            raise RuntimeError('Complete original audience changed; recapture/review required')
        for selection, original in zip(self.audience, self.originals, strict=True):
            current = selection.require_current(snapshot)
            current.require_idle()
            if current != original:
                raise RuntimeError('Original configuration changed')
        self.require_sources()
        InputDispositions(ROOT / InputDispositions.filename).read()
        require_publication_preimages(self.original_route, self.current)
        require_no_clients(self.audience)

    def retain_preimages(self):
        directory = self.receipt.with_suffix('.originals')
        directory.mkdir(mode=0o700)
        originals = []
        for source in self.candidate:
            archive = Path(source.root)
            destination = directory / archive.name
            destination.mkdir(mode=0o700)
            for name in ('bus.jsonl', 'registry.json', 'bus_meta.json', 'private_bus_checkpoint.sqlite3',
                         'private_bus_checkpoint.sqlite3-journal', 'private_bus_checkpoint.sqlite3-wal',
                         'private_bus_checkpoint.sqlite3-shm'):
                path = archive / name
                if path.exists():
                    originals.append(retain_file(path, destination / name))
            fsync_directory(destination)
        for name in (HistoryArchive.filename, InputDispositions.filename, 'bus_meta.json', 'registry.json',
                     'coordination.sqlite3', 'coordination.sqlite3-wal', 'coordination.sqlite3-shm'):
            path = ROOT / name
            if path.exists():
                originals.append(retain_file(path, directory / name))
        fsync_directory(directory)
        self.note('original-preimages-retained-before-derived-reset', original_files=originals)

    def protected_originals(self):
        """Read original file observations, never mint or infer input dispositions."""
        paths = {ROOT / 'bus.jsonl'}
        for name in (InputDispositions.filename, 'coordination.sqlite3', 'coordination.sqlite3-wal',
                     'goal_history.sqlite3', 'goal_waits.json'):
            path = ROOT / name
            if path.exists():
                paths.add(path)
        for original in self.originals:
            if original.session_file is not None:
                session = Path(original.session_file)
                paths.add(session)
                for suffix in ('.input-proof', '.input-proof-journal', '.input-proof-wal', '.input-proof-shm'):
                    proof = Path(str(session) + suffix)
                    if proof.exists():
                        paths.add(proof)
        return {str(path): digest(path) for path in sorted(paths)}

    def after_stopped(self, lifecycle):
        # The canonical batch holds .wire.lock and proves all exact original exits.
        require_publication_preimages(self.original_route, self.current)
        require_no_clients(self.audience)
        snapshot = lifecycle.registry.snapshot()
        for original in self.originals:
            if snapshot.threads[original.name] != original:
                raise RuntimeError('Original settings changed during retirement')
        self.require_sources()
        self.retain_preimages()
        protected = self.protected_originals()
        self.note('all-original-owners-stopped-original-writer-installation')
        for source in self.candidate:
            subprocess.run([str(OLD_WRITER), str(WRITER), source.root,
                            str(TARGET / 'bin/python'), str(INSTALLER), source.wire_root_id],
                           env=clean_environment(), check=True)
            source.validate()  # Original bus/registry revisions are invariant.
        # The sole carry owner validates the original descriptor again after
        # checkpoint seals change; its semantic provenance must remain identical.
        carried, final_proof = carry(self.manifest)
        if FieldCodec.decode(tuple[HistorySource, ...], carried) != self.candidate:
            raise RuntimeError('Determining provenance changed during index installation')
        if self.manifest.read_bytes() != self.manifest_bytes:
            raise RuntimeError('Manifest CAS preimage changed')
        _atomic_write_text(self.manifest, json.dumps(carried, indent=2)+'\n', fsync_parent=True)
        if self.protected_originals() != protected:
            raise RuntimeError('Protected input/native/wire/goal bytes changed before target launch')
        self.note('original-provenance-published', provenance_carry=final_proof)
        self.note('protected-originals-unchanged-before-target-launch', protected_original_sha256=protected)
        target_route = replace(self.original_route, native_package=NATIVE)
        _publish_active_route_locked(target_route, active_route_path(), self.route_directory,
                                     expected=self.original_route)
        for command in COMMANDS:
            link = LINKS / command
            if link.readlink() != self.current / 'bin' / command:
                raise RuntimeError('Default changed during publication; remain stopped')
            temporary = LINKS / (command + '.openhcs-recovery-publish')
            temporary.symlink_to(TARGET / 'bin' / command)
            temporary.replace(link)
        descriptor = os.open(LINKS, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        if read_active_route() != target_route:
            raise RuntimeError('Target route readback differs')
        self.note('target-route-and-defaults-published-before-retained-launch')
        # complete() invokes the sole original credential/settings handoff and
        # verifies it before FencedOwnerBatch releases original wire custody.


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--receipt', type=Path, required=True)
    parser.add_argument('--source-interpreter', type=Path, required=True)
    parser.add_argument('--current-prefix', type=Path, required=True)
    for artifact in ('activation', 'source-proof', 'writer', 'installer', 'schema-script'):
        parser.add_argument('--'+artifact+'-sha256', required=True)
    for gate in ('ui', 'archive'):
        parser.add_argument('--'+gate+'-gate-artifact', type=Path)
        parser.add_argument('--'+gate+'-gate-sha256')
    args = parser.parse_args()
    if args.receipt.exists() or args.receipt.is_symlink():
        raise RuntimeError('Existing attempt needs review; never automatically replay it')
    require_stage(args)
    original = read_active_route()
    if original != ActiveRoute(ROOT, ROOT_ID, NATIVE):
        raise RuntimeError('Original route/root/native differs from reviewed release')
    if not args.source_interpreter.is_absolute() or not args.source_interpreter.is_file():
        raise RuntimeError('Authentic source interpreter is required')
    manifest = ROOT / HistoryArchive.filename
    manifest_bytes = manifest.read_bytes()
    carried, carry_proof = carry(manifest)
    sources = FieldCodec.decode(tuple[HistorySource, ...], carried)
    if {Path(source.root).name: source.wire_root_id for source in sources} != ARCHIVES:
        raise RuntimeError('Original archive audience changed; review actual carry scope')
    if any(Path(source.root).parent != ROOT / 'history' for source in sources):
        raise RuntimeError('Archive source names another root')
    service = Comms(ROOT, private_initial_writes=False, private_claim_writes=False)
    snapshot = service.registry.snapshot()
    owners = tuple(OwnerRestartRequest().threads(snapshot))
    if not owners:
        raise RuntimeError('Empty original owner audience requires review')
    audience = tuple(OwnerRestartSelection.capture(snapshot, thread.name) for thread in owners)
    for thread in owners:
        thread.require_idle()
        RetainedOwnerLaunch.capture(thread, snapshot, interpreter=str(args.source_interpreter))
    directory = os.open(active_route_path().parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        fcntl.flock(directory, fcntl.LOCK_EX | fcntl.LOCK_NB)
        operation = PublishOpenhcsRecovery(audience, owners, original, directory,
                                         args.current_prefix, args.receipt, manifest_bytes,
                                         sources, carry_proof)
        operation.require_selection(snapshot, owners)
        print(json.dumps({'state':'read-only-preflight', 'owners':len(owners), 'clients':0,
                          'source_interpreter':str(args.source_interpreter)}), flush=True)
        if not args.execute:
            return
        gates = {}
        for label, artifact, expected in (('ui',args.ui_gate_artifact,args.ui_gate_sha256),
                                         ('archive',args.archive_gate_artifact,args.archive_gate_sha256)):
            if artifact is None or expected is None:
                raise RuntimeError('Execution requires BOTH reviewed actual gate artifacts and hashes')
            if artifact in (SOURCE_PROOF, TARGET / 'activation.json') or digest(artifact) != expected:
                raise RuntimeError('Actual journey evidence required, distinct from package metadata')
            gates[label] = {'artifact':str(artifact), 'sha256':expected}
        args.receipt.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if args.receipt.parent.stat().st_mode & 0o077:
            raise RuntimeError('Receipt directory must be private0700')
        descriptor = os.open(args.receipt, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor,'w') as opened:
            json.dump({'phase':'preflight-complete', 'started':time.time(), 'target':str(TARGET),
                       'pins':PINS, 'actual_gate_artifacts':gates,
                       'owners_before':FieldCodec.encode(audience)}, opened,indent=2)
            opened.flush()
            os.fsync(opened.fileno())
        runtime = RestartEnvironment(path=str(TARGET / 'bin')+':'+os.environ['PATH'], virtual_env=str(TARGET))
        results = service.owners.restart_owners(runtime=runtime,
            source_interpreter=str(args.source_interpreter), cutover=operation)
        print(json.dumps({'state':'published-and-launched','owners':len(results)}),flush=True)
    finally:
        os.close(directory)


if __name__ == '__main__':
    main()
