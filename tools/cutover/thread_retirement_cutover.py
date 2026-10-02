"""Original typed admission, both durable carriers, target publication and launch.

The canonical OwnerCutover owns the only stop/start procedure. This one-shot
member changes the original documents only after its acquired stopped phase.
Credentials cross a private subprocess stdin pipe in RAM, never files/argv.
"""
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path

from agent_comms.errors import RelationViolationError
from agent_comms.field_codec import FieldCodec
from agent_comms.owner_cutover import OwnerCutover
from agent_comms.owner_restart import OwnerRestartRequest
from agent_comms.store_files import _atomic_write_text
from thread_format_retirement import GoalReportMemberRetirement
from cutover_child import run_cutover_child


@dataclass(frozen=True)
class ThreadRetirementCutover(OwnerCutover):
    target_python: Path
    target_environment: dict[str, str] = field(repr=False)
    route_path: Path
    route_descriptor: int
    original_route: dict
    target_route: dict
    receipt: Path

    def failed(self, failure):
        # The child may have committed target-format bytes. A source decoder
        # cannot recover merely because target completion failed.
        self.leave_stopped(failure)

    def validate(self, registry, releases):
        projected = {'registry': GoalReportMemberRetirement.threads(FieldCodec.encode(registry)),
                     'releases': GoalReportMemberRetirement.releases(FieldCodec.encode(releases))}
        result = run_cutover_child([
            str(self.target_python), str(Path(__file__).with_name('validate_thread_retirement.py')),
        ], packet=json.dumps(projected), environment=self.target_environment)
        return projected, json.loads(result.stdout)

    def require_selection(self, snapshot, owners):
        live = {thread.name for thread in OwnerRestartRequest().threads(snapshot)}
        if {thread.name for thread in owners} != live:
            raise RelationViolationError('Thread retirement requires the complete live batch')
        if not self.receipt.is_absolute() or self.receipt.exists() or self.receipt.is_symlink():
            raise RelationViolationError('Thread retirement requires a fresh private receipt')
        parent = self.receipt.parent.stat()
        if parent.st_uid != os.geteuid() or parent.st_mode & 0o077:
            raise RelationViolationError('Thread retirement receipt directory must be owner-only')
        if self.originals.exists() or self.originals.is_symlink():
            raise RelationViolationError('Original recovery custody forbids repeating retirement')

    @property
    def originals(self):
        return self.receipt.with_name(self.receipt.name + '.originals')

    def complete(self, stopped):
        lifecycle = stopped.lifecycle
        with lifecycle.registry.store.locked(), lifecycle.releases.locked():
            registry = lifecycle.registry.store._read_unlocked()
            releases = lifecycle.releases._read_unlocked()
            projected, counts = self.validate(registry, releases)
            # Complete target decoding succeeds before any source-byte change.
            sources = ((lifecycle.registry.store, 'registry.json'),
                       (lifecycle.releases, 'owner_release_receipts.json'))
            originals = {name: store.path.read_bytes() if store.path.exists() else None
                         for store, name in sources}
            originals['active-route.json'] = self.route_path.read_bytes()
            self.originals.mkdir(mode=0o700)
            for name, contents in originals.items():
                if contents is not None:
                    _atomic_write_text(self.originals / name, contents.decode(), fsync_parent=True)
                    os.chmod(self.originals / name, 0o600)
            proof = {'originals': {name: hashlib.sha256(contents).hexdigest()
                                  if contents is not None else None
                                  for name, contents in originals.items()},
                     'target_validation': counts,
                     'all_original_processes_retired': True, 'provider_calls': 0,
                     'original_input_replays': 0, 'phase': 'originals_retained'}
            _atomic_write_text(self.receipt, json.dumps(proof, indent=2), fsync_parent=True)
            try:
                lifecycle.registry.store._write_unlocked(json.dumps(projected['registry']))
                if originals['owner_release_receipts.json'] is not None:
                    lifecycle.releases._write_unlocked(json.dumps(projected['releases']))
            except BaseException:
                # A failed guarded write may deliberately forbid rollback.
                # Preserve its private preimages and stopped batch for review.
                for store, name in sources:
                    contents = originals[name]
                    if contents is not None:
                        store._write_unlocked(contents.decode())
                raise
        proof['phase'] = 'target_format_installed'
        _atomic_write_text(self.receipt, json.dumps(proof, indent=2), fsync_parent=True)
        packet = {'handoff': FieldCodec.encode(stopped.handoff),
                  'route_path': str(self.route_path), 'original_route': self.original_route,
                  'target_route': self.target_route, 'receipt': str(self.receipt)}
        result = run_cutover_child([
            str(self.target_python), str(Path(__file__).with_name('launch_thread_retirement.py')),
            str(stopped.wire.descriptor), str(self.route_descriptor),
        ], packet=json.dumps(packet), environment=self.target_environment,
            descriptors=(stopped.wire.descriptor, self.route_descriptor))
        from agent_comms.owner_lifecycle import OwnerRestartResult

        return FieldCodec.decode(tuple[OwnerRestartResult, ...], json.loads(result.stdout))
