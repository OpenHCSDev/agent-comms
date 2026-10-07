"""Authentic original-runtime fixture process; no native/provider work."""
from dataclasses import replace
import json
import os
from pathlib import Path
import sys

from agent_comms.child_process import ProcessIdentity
from agent_comms.comms import Comms
from agent_comms.goal_actions import BlockedGoalAction, ModelInvocable, SetGoalAction
from agent_comms.turn_lease import ActiveTurn
from agent_comms.owner_lifecycle import OwnerReleaseReceipt, OwnerReleaseStore
from agent_comms.thread_status import StoppedThreadStatus
from agent_comms.threads import Thread


def author_model_report(service, name, turn):
    """Original actions and registry commits author the preserved report fact."""
    service.goals.update_goal(name, SetGoalAction(text='Private original report fixture'))
    source = service.registry.require(name)
    service.registry.register(replace(source, active_turn=ActiveTurn(turn, source.pid)))
    service.goals.update_goal(name, BlockedGoalAction(block_reason='No original work replay'),
                              actor=ModelInvocable)
    source = service.registry.require(name)
    service.registry.register(replace(source, active_turn=None))


def main():
    root = Path(sys.argv[1])
    root.mkdir(mode=0o700)
    service = Comms(root)
    registry = service.registry
    source = Thread('captured-original', frozenset({'original'}), str(root),
                    process_identity=ProcessIdentity.capture(os.getpid()),
                    task='Private capture fixture only')
    registry.register(source, new_owner=True)
    author_model_report(service, source.name, 'c7cc88d777b947d99b6f28f9e0b6ef97')
    retired = Thread('retired-original', frozenset(), str(root))
    registry.register(retired, StoppedThreadStatus(), new_owner=True)
    author_model_report(service, retired.name, 'd7cc88d777b947d99b6f28f9e0b6ef97')
    retired = registry.require(retired.name)
    OwnerReleaseStore(root / 'owner_release_receipts.json').replace({
        retired.name: OwnerReleaseReceipt(1, 2, retired),
    })
    print(json.dumps({'pid': os.getpid(), 'root': str(root)}), flush=True)
    for command in sys.stdin:
        if command.strip() == 'advance-admission':
            # Same owner generation and process, a distinct admission domain.
            with registry.store.editing() as edit:
                edit.document.admissions.advance(source.name)
                edit.commit()
            print('advanced', flush=True)
        else:
            break


def runtime_owners(root, package):
    """Seed real idle source-runtime workers and SDK-authored saved history."""
    import asyncio
    from agent_comms.fresh_private_session import create_fresh_private_session
    from agent_comms.goal_states import BlockedGoal
    from agent_comms.goals import Goal
    from native_backend_fixture import NativeBackendFixture
    from seed_thread_retirement_fixture import ready

    root.mkdir(mode=0o700)
    service = Comms(root, private_initial_writes=True)
    root_id = service.messaging.initialize_private_initial_protocol()
    service.owners.pin_private_nk_launch(root, root_id, package)
    os.environ.update(AGENT_COMMS_ROOT=str(root),
        AGENT_COMMS_PRIVATE_NK_WIRE_ROOT_ID=root_id,
        AGENT_COMMS_PRIVATE_NK_NATIVE_PACKAGE=str(package),
        PI_COMPACTION_TEST_PACKAGE=str(package))
    names = ('replacement-alpha', 'replacement-beta')
    try:
        for index, name in enumerate(names):
            project = root.parent / name
            project.mkdir(mode=0o700)
            saved = create_fresh_private_session(project / 'sessions', worktree=project)
            fixture = NativeBackendFixture(root, project, saved.path, None, project)
            asyncio.run(fixture.author_history())
            service.registry.declare(Thread(name, frozenset({name}), str(project),
                session_file=str(saved.path), model='response-local/fixture', thinking_level='off',
                task='Idle private replacement fixture',
                goal=Goal('Do not invoke provider', name+'-goal', state=BlockedGoal('No input'))))
            os.environ.update(PI_CODING_AGENT_DIR=str(project / 'config'),
                              BATCH_OWNER_CREDENTIAL='private-'+str(index))
            service.owners.start(name, agent_args=('--offline', '--no-tools') if index == 0 else ())
            asyncio.run(ready(root, service.registry.require(name)))
        print(json.dumps({'root_id':root_id, 'names':names}), flush=True)
        # The operator replaces workers while this source fixture retains cleanup.
        sys.stdin.readline()
    finally:
        for name in names:
            if name in service.registry.snapshot().threads:
                service.owners.stop(name)


if __name__ == '__main__':
    if sys.argv[1] == '--runtime-owners':
        runtime_owners(Path(sys.argv[2]), Path(sys.argv[3]))
    else:
        main()
