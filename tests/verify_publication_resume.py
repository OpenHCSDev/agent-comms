"""Run P753's failed stopped publication and resume on a bounded real bus copy."""
from dataclasses import asdict
import json
import os
from pathlib import Path
import shutil
import sys
from uuid import uuid4
from unittest.mock import patch

from agent_comms.active_route import ActiveRoute, publish_active_route
from agent_comms.check_root import CheckRoot
from agent_comms.comms import Comms
from agent_comms.launch_runtime import LaunchRuntime
from agent_comms.owner_restart import StoppedOwnerFailure
from agent_comms.harness_install import replace_link
from cutover.backup import StoreBackup
from cutover.carry import CarryChain, CarryLedger, PendingConversion, PreparedCarries
from install_gate import prepare_runtime_home
from publish_runtime import HeldRuntimePublication, PublicationIntent, RuntimePublication
from upgrade_bus import Stack


def main():
    base = Path('/home/ts/.cache/agent-scratch/core-runtime-location/P753')
    checkout = Path(__file__).resolve().parents[1]
    source = Comms(Path('/var/tmp/agent-comms-live-20260927-wzjtqhza'))
    units = ('core-read-locks-2', 'core-goal-retry')
    target = Path('/home/ts/.local/share/agent-comms/current').resolve()
    previous = Path(sys.executable).parent.parent.resolve()
    run = uuid4().hex
    home = base / f'private-home-{run}'
    home.mkdir(mode=0o700)
    prepare_runtime_home(home)
    with CheckRoot(source, units, base / f'bus-copy-{run}').open() as copied:
        shutil.copy2(source.root / 'carries.json', copied.root / 'carries.json')
        chain = CarryChain.at(checkout, 'HEAD')
        assert not chain.pending(CarryLedger(copied.root).applied())
        # Force the actual stopped publication path, without changing any stores.
        conversion = PendingConversion(chain, (), StoreBackup(
            copied.root, home / '.local/state/agent-comms/active-route.json',
            base / 'conversion-backup'), checkout)
        original_home = os.environ['HOME']
        os.environ['HOME'] = str(home)
        try:
            current = LaunchRuntime.current_pointer()
            current.parent.mkdir(parents=True, exist_ok=True)
            replace_link(current, previous)
            marker = copied.bus.log.private_marker_unlocked()
            publish_active_route(ActiveRoute(copied.root, marker.root_id))
            stack = Stack(checkout, home=home, runtime=LaunchRuntime(target / 'bin/python'))
            publication = RuntimePublication(target, {})
            stack.owners(copied.root, 'start', [units[0]])
            first_process = copied.registry.require(units[0]).process_identity
            def fail_stopped(self, stopped):
                assert not copied.registry.require(units[0]).process_alive
                raise RuntimeError('P753 controlled failure after stopping')
            with patch.object(PreparedCarries, 'at', return_value=conversion):
                with patch.object(HeldRuntimePublication, 'complete', fail_stopped):
                    try:
                        publication.publish(copied.root, checkout=checkout)
                    except StoppedOwnerFailure as error:
                        assert 'P753 controlled failure after stopping' in str(error)
                    else:
                        raise AssertionError('Controlled stopped failure did not run')
                intent = PublicationIntent.read(copied.root / 'runtime-publication.json')
                assert tuple(owner.name for owner in intent.owners) == (units[0],)
                stack.owners(copied.root, 'start', [units[1]])
                added_process = copied.registry.require(units[1]).process_identity
                resumed = publication.publish(copied.root, checkout=checkout)
            added = copied.registry.require(units[1])
            assert units[1] in resumed and added.process_alive
            assert copied.registry.require(units[0]).process_alive
            assert copied.registry.require(units[0]).process_identity != first_process
            assert added.process_identity != added_process
            assert not (copied.root / 'runtime-publication.json').exists()
            result = {'resumed_selection': resumed, 'additional_owner': units[1],
                      'before_resume': asdict(added_process),
                      'after_resume': asdict(added.process_identity),
                      'additional_owner_running': added.process_alive,
                      'original_owner_running': copied.registry.require(units[0]).process_alive,
                      'original_process': asdict(first_process),
                      'private_pointer': str(current.resolve()),
                      'live_installation_changed': False}
            (base / 'bus-copy-result.json').write_text(json.dumps(result, indent=2))
            print(json.dumps(result, indent=2), flush=True)
            stack.owners(copied.root, 'stop', list(units))
        finally:
            os.environ['HOME'] = original_home
            shutil.rmtree(home)


if __name__ == '__main__':
    main()
