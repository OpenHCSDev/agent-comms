"""Carry the claim index once, with old writers stopped; delete after cutover.

Default is a read-only plan. The existing canonical installer certifies the
unchanged bus and atomically replaces its checkpoint/seal. No old row decoder
or reader fallback is installed in production source.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import closing
from dataclasses import dataclass, replace
import json
import os
from pathlib import Path
import shutil
import sys

from agent_comms.child_process import Platform
from agent_comms.active_route import ActiveRoute
from agent_comms.coordination_database import CoordinationStore
from agent_comms.errors import RelationViolationError
from agent_comms.maintenance_barrier import MaintenanceState
from agent_comms.owner_cutover import MaintainedOwnerCutover
from agent_comms.private_bus_checkpoint import (
    CheckpointTable, PrefixCertificate, _path,
)
from agent_comms.private_path import FileRevision
from agent_comms.store_files import _store_lock_file
from agent_comms.typed_table import SQLiteSchemaObject, TypedTable
from agent_comms.wire_log import WireLog


def carry(bus_path: Path, *, apply: bool = False, backup: Path | None = None) -> dict:
    from agent_comms.private_bus_checkpoint import ClaimCommits, ResourceClaims

    bus = WireLog(bus_path)
    if bus.path.name != "bus.jsonl" or not bus.path.is_file():
        raise ValueError("An existing canonical private bus.jsonl is required")
    checkpoint = _path(bus.path)
    platform = Platform.current()
    # The new ordinary reader intentionally rejects the old schema. Borrow
    # only the existing physical lock implementation for this offline carry.
    with _store_lock_file(bus.path) as lock_file:
        platform.acquire_store_lock(lock_file.fileno(), shared=False, blocking=False)
        try:
            marker = bus._private_marker_unlocked()
            if not marker.claims:
                raise RelationViolationError("Resource claim carry requires a private claim bus")
            with closing(CoordinationStore.read_snapshot(checkpoint)) as db:
                original = PrefixCertificate.read_witness(db)
                marker.seal.check_final(original, checkpoint)
                original.require_marker(marker)
                if FileRevision.from_stat(bus.path.stat()) != original.revision:
                    raise RelationViolationError("Bus differs from its sealed original prefix")
                actual = SQLiteSchemaObject.read(db.execute(
                    "SELECT name,sql FROM sqlite_master WHERE sql IS NOT NULL"
                ))
            target = {name: sql for row in TypedTable.members_with(CheckpointTable)
                      for name, sql in row.schema_objects().items()}
            additions = {name: sql for row in (ClaimCommits, ResourceClaims)
                         for name, sql in row.schema_objects().items()}
            preceding = {name: sql for name, sql in target.items() if name not in additions}
            already_carried = not SQLiteSchemaObject.differences(actual, target)
            if not already_carried and SQLiteSchemaObject.differences(actual, preceding):
                raise RelationViolationError("Foreign, changed or partially carried checkpoint schema")
            plan = {
                "bus": str(bus.path), "through_sequence": original.through_seq,
                "bus_bytes": original.offset, "original_digest": original.digest,
                "new_objects": [] if already_carried else sorted(additions),
                "already_carried": already_carried, "applied": False,
                "bus_bytes_rewritten": 0,
            }
            if apply and backup is not None and already_carried:
                raise RelationViolationError('Claim migration already applied; review the original installation')
            if not apply or already_carried:
                return plan
            if backup is not None:
                backup.mkdir(mode=0o700)
                for path in (checkpoint, bus.metadata_path):
                    destination = backup / path.name
                    shutil.copy2(path, destination)
                    with destination.open('rb') as saved:
                        os.fsync(saved.fileno())
                for directory in (backup, backup.parent):
                    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
                    try:
                        os.fsync(descriptor)
                    finally:
                        os.close(descriptor)
            expected_marker = replace(marker, checkpoint_version=None, checkpoint_seal=None)
            try:
                carried = PrefixCertificate._install(bus, marker)
                current_marker = bus._private_marker_unlocked()
                if (carried != original or
                    replace(current_marker, checkpoint_version=None, checkpoint_seal=None) != expected_marker):
                    raise RelationViolationError("Carry changed original bytes or non-seal marker facts")
            except Exception as error:
                raise RelationViolationError(
                    "Claim-index carry outcome UNKNOWN; preserve this root, do not retry"
                ) from error
            plan["applied"] = True
            return plan
        finally:
            platform.release_store_lock(lock_file.fileno())


@dataclass
class InstallResourceClaims(MaintainedOwnerCutover):
    """Use the existing stopped batch; only its final handoff crosses runtimes."""

    route: ActiveRoute
    target: Path
    backup: Path
    maintenance_hold: MaintenanceState

    def require_selection(self, snapshot, owners):
        from agent_comms.active_route import read_active_route
        from agent_comms.owner_restart import OwnerRestartRequest

        if read_active_route() != self.route:
            raise RelationViolationError('Route changed before claim migration')
        if not owners or {owner.name for owner in owners} != {
            owner.name for owner in OwnerRestartRequest().threads(snapshot)
        }:
            raise RelationViolationError('Claim migration requires the complete worker batch')
        if self.backup.exists():
            raise RelationViolationError('Original migration attempt already exists; do not repeat')

    def require_launch(self, lifecycle):
        lifecycle.require_launch_authority()

    def failed(self, failure):
        self.leave_stopped(failure)

    def complete(self, stopped):
        from agent_comms.active_route import active_route_path, acquire_exclusive_route_lock
        from agent_comms.field_codec import FieldCodec
        from agent_comms.owner_lifecycle import OwnerRestartResult
        from cutover_child import run_cutover_child
        from publish_openhcs_recovery import require_no_clients

        # Managed observers release their route locks when the batch stops.
        directory = os.open(active_route_path().parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            acquire_exclusive_route_lock(directory)
            require_no_clients(())
            packet = dict(handoff=FieldCodec.encode(stopped.handoff),
                          route=FieldCodec.encode(self.route),
                          maintenance=FieldCodec.encode(self.maintenance_hold),
                          previous_runtime=str(Path(sys.executable).parent.parent),
                          backup=str(self.backup))
            environment = dict(os.environ)
            environment.pop('PYTHONPATH', None)
            result = run_cutover_child(
                [str(self.target / 'bin/python'), str(Path(__file__).resolve()),
                 '--finish', str(stopped.wire_descriptor), str(directory)],
                environment=environment, packet=json.dumps(packet),
                descriptors=(stopped.wire_descriptor, directory),
            )
            return FieldCodec.decode(tuple[OwnerRestartResult, ...], json.loads(result.stdout)['workers'])
        finally:
            os.close(directory)


def finish_install(wire_descriptor: int, route_descriptor: int) -> None:
    """New-format operations run only after the original processes have exited."""
    import fcntl
    from agent_comms.active_route import read_active_route
    from agent_comms.comms import Comms
    from agent_comms.field_codec import FieldCodec
    from agent_comms.owner_restart import OwnerRestartHandoff, StoppedOwnerBatch
    from agent_comms.store_files import _atomic_write_text
    from publish_runtime import RuntimePublication

    packet = json.load(sys.stdin)
    route = FieldCodec.decode(ActiveRoute, packet['route'])
    handoff = FieldCodec.decode(OwnerRestartHandoff, packet['handoff'])
    handoff = replace(handoff, runtime=handoff.runtime.for_interpreter(Path(sys.executable)))
    hold = FieldCodec.decode(MaintenanceState, packet['maintenance'])
    publication = RuntimePublication.installed()
    if read_active_route() != route or publication.current.resolve() != Path(packet['previous_runtime']):
        raise RelationViolationError('Original route/runtime changed before migration')
    service = Comms(route, private_initial_writes=False, private_claim_writes=False)
    stopped = StoppedOwnerBatch.accept(service.owners, wire_descriptor, handoff)
    service.owners.maintenance.require_draining_unlocked(hold)
    snapshot = service.registry.snapshot()
    for owner in handoff.owners:
        owner.require_current(snapshot)
    backup = Path(packet['backup'])
    result = carry(route.root / 'bus.jsonl', apply=True, backup=backup)
    route.bind_owners(service.owners)
    service.owners.require_launch_authority()
    publication.publish()
    _atomic_write_text(backup / 'installation.json', json.dumps(
        dict(runtime=str(publication.runtime), migration=result, workers_launched=False), indent=2),
        fsync_parent=True)
    service.owners.maintenance.clear_stopped(hold, wire_descriptor)
    # New workers need the route; the original wire descriptor still excludes
    # changes while the shared handoff revalidates every original before launch.
    fcntl.flock(route_descriptor, fcntl.LOCK_UN)
    workers = stopped.launch()
    completed = dict(runtime=str(publication.runtime), migration=result,
                     workers=FieldCodec.encode(workers))
    _atomic_write_text(backup / 'installation.json', json.dumps(completed, indent=2), fsync_parent=True)
    print(json.dumps(completed), flush=True)


async def install_runtime(target: Path, backup: Path) -> None:
    from contextlib import aclosing
    from agent_comms.active_route import read_active_route
    from agent_comms.comms import wire
    from agent_comms.field_codec import FieldCodec
    from agent_comms.maintenance_barrier import MaintenanceOperatorField
    from agent_comms.owner_launch import RestartEnvironment
    from agent_comms.owner_restart import OwnerRestartRequest
    from agent_comms.restart_queue import _records
    from agent_comms.restart_refusals import OwnerBusyRefusal
    from agent_comms.wire_watch import WatchSource, WireWatch
    from publish_openhcs_recovery import ROOT

    route = read_active_route()
    source = Path(sys.executable).parent.parent
    if route is None or source != (Path.home() / '.local/share/agent-comms/current').resolve():
        raise RelationViolationError('Run migration through the exact current installed interpreter')
    if route.root != ROOT:
        raise RelationViolationError('Client exclusion must name the actual migration root')
    if (not target.is_absolute() or target.resolve() != target
            or not (target / 'bin/python').is_file() or target == source):
        raise ValueError('A distinct reviewed installed target is required')
    if not backup.is_absolute() or backup.exists() or not backup.parent.is_dir():
        raise ValueError('A fresh backup directory under an existing parent is required')
    service = wire(route)
    service.owners.require_launch_authority()
    if any(record.state.active for _, record in _records(service)):
        raise RelationViolationError('Finish or retire pending idle reloads before this installation')
    gate = service.owners.maintenance
    hold = gate.begin(FieldCodec.decode(MaintenanceOperatorField, 'resource-claims-migration'))
    operation = InstallResourceClaims(route, target, backup, hold)
    runtime = RestartEnvironment.inherit(os.environ).for_interpreter(target / 'bin/python')

    def busy_workers():
        names = []
        for owner in OwnerRestartRequest().threads(service.registry.snapshot()):
            try:
                service.owners.require_restart_idle(owner)
            except OwnerBusyRefusal:
                names.append(owner.name)
        return names

    previous = None
    async with aclosing(WireWatch.observations(
        WatchSource.file(route.root / 'registry.json'),
        WatchSource.modified_file(route.root / 'coordination.sqlite3'),
        WatchSource.modified_file(route.root / 'coordination.sqlite3-wal'),
    )) as changes:
        async for _ in changes:
            busy = await asyncio.to_thread(busy_workers)
            if busy != previous:
                print(json.dumps(dict(finishing_current_work=busy)), flush=True)
                previous = busy
            if not busy:
                break
    results = await asyncio.to_thread(service.owners.restart_owners, runtime=runtime, cutover=operation)
    print(json.dumps(dict(installed=str(target), workers=FieldCodec.encode(results))), flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bus", type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument('--install-runtime', type=Path)
    parser.add_argument('--backup', type=Path)
    parser.add_argument('--finish', nargs=2, type=int)
    args = parser.parse_args()
    if args.finish:
        finish_install(*args.finish)
    elif args.install_runtime:
        if args.backup is None or args.apply or args.bus is not None:
            parser.error('--install-runtime requires --backup and excludes --bus/--apply')
        asyncio.run(install_runtime(args.install_runtime, args.backup))
    else:
        if args.bus is None:
            parser.error('--bus is required for plan/apply')
        print(json.dumps(carry(args.bus, apply=args.apply, backup=args.backup), indent=2))


if __name__ == "__main__":
    main()
