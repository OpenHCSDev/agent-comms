"""Authentic original-runtime fixture process; no native/provider work."""
import json
import os
from pathlib import Path
import sys

from agent_comms.child_process import ProcessIdentity
from agent_comms.owner_lifecycle import OwnerReleaseReceipt, OwnerReleaseStore
from agent_comms.registration import Registration
from agent_comms.thread_status import StoppedThreadStatus
from agent_comms.threads import Thread


def main():
    root = Path(sys.argv[1])
    root.mkdir(mode=0o700)
    registry = Registration(root / 'registry.json')
    source = Thread('captured-original', frozenset({'original'}), str(root),
                    process_identity=ProcessIdentity.capture(os.getpid()),
                    last_goal_report_turn='original-report-7', task='Private capture fixture only')
    registry.register(source, new_owner=True)
    retired = Thread('retired-original', frozenset(), str(root),
                     last_goal_report_turn='original-report-8')
    registry.register(retired, StoppedThreadStatus(), new_owner=True)
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
