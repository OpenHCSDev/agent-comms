"""One-shot target half: inherit stopped custody, carry stores, publish route, launch.

The original-format process owns the wire and route locks throughout. No input
is sent or retried here. Handoff credentials arrive only on stdin and stay in RAM.
"""
from __future__ import annotations

from dataclasses import replace
import importlib.util
import json
import os
from pathlib import Path
import sys

from agent_comms.active_route import ActiveRoute, _publish_active_route_locked, read_active_route
from agent_comms.comms import Comms
from agent_comms.field_codec import FieldCodec
from agent_comms.launch_runtime import LaunchRuntime, RestartEnvironment
from agent_comms.maintenance_barrier import MaintenanceBarrier, MaintenanceState
from agent_comms.native_package import pinned_native_package, verify_native_package
from agent_comms.owner_restart import OwnerRestartHandoff, StoppedOwnerBatch


def main():
    wire_fd, route_fd = map(int, sys.argv[1:3])
    payload = json.load(sys.stdin)
    handoff = FieldCodec.decode(OwnerRestartHandoff, payload['handoff'])
    hold = FieldCodec.decode(MaintenanceState, payload['maintenance'])
    root = Path(handoff.root)
    repo = Path('/home/ts/code/projects/agent-comms')
    receipt = Path(sys.argv[3])
    old_route = read_active_route()
    assert old_route is not None and old_route.root == root
    package = pinned_native_package()
    verify_native_package(package)
    # The original-format parent retains the actual stopped batch and wire.
    # Do not construct new Registration until its old records have been carried.
    stages = []
    for name in ('carry_storage_roots', 'carry_goal_publications', 'carry_goal_wait_owners'):
        path = repo / 'tools/cutover' / f'{name}.py'
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        stages.append({'stage': name, **module.carry_locked(root, apply=True)})
    comms = Comms(root)
    stopped = StoppedOwnerBatch.accept(comms.owners, wire_fd, handoff)
    snapshot = comms.registry.snapshot()
    for owner in handoff.owners:
        owner.require_current(snapshot)
    route = ActiveRoute(root, old_route.wire_root_id, package)
    _publish_active_route_locked(route, Path.home() / '.local/state/agent-comms/active-route.json',
                                 route_fd, expected=old_route)
    comms.owners.pin_private_nk_launch(root, route.wire_root_id, package)
    runtime = LaunchRuntime.current()
    environment = runtime.environment(os.environ)
    environment.update(AGENT_COMMS_ROOT=str(root), AGENT_COMMS_PRIVATE_NK_WIRE_ROOT_ID=route.wire_root_id,
                       AGENT_COMMS_PRIVATE_NK_NATIVE_PACKAGE=str(package))
    target = replace(handoff, runtime=RestartEnvironment.inherit(environment).without_pythonpath(),
                     launch_runtime=runtime)
    MaintenanceBarrier(root / 'registry.json').clear_stopped(hold, wire_fd)
    results = target.launch(comms.owners, runtime)
    outcome = {'state': 'installed', 'root': str(root), 'native_package': str(package),
               'core': __import__('subprocess').check_output(['git', '-C', str(repo), 'rev-parse', 'HEAD'], text=True).strip(),
               'migration': stages, 'restarted': [FieldCodec.encode(result) for result in results]}
    receipt.write_text(json.dumps(outcome, indent=2))
    print(json.dumps(outcome), flush=True)


if __name__ == '__main__':
    main()
