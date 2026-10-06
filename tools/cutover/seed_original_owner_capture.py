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


if __name__ == '__main__':
    main()
