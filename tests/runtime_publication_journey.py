"""Provider-free real-worker publication journey on an isolated private root.

The controlled active turn is allocated/released by the registry owner. It is
not a model request, does not replay a saved input, and does not touch live data.
Run with the qualified target Python and an output directory under owned scratch.
Prepare output/wire with tools/upgrade_bus.py first; this journey never skips carries.
"""
from pathlib import Path
import json
import os
import signal
import subprocess
import sys
import threading
import time

source = Path(__file__).resolve().parents[1]
from agent_comms.active_route import ActiveRoute, publish_active_route
from agent_comms.comms import Comms
from agent_comms.owner_cutover import StoppedOwnerInstallation
from agent_comms.maintenance_barrier import ReadyPhase
from agent_comms.runtime import socket_path
from tools.publish_runtime import RuntimePublication
from agent_comms.carry import CarryChain, CarryLedger


def main():
    output, old, target = map(Path, sys.argv[1:])
    output.mkdir(mode=0o700, parents=True, exist_ok=True)
    root = output / 'wire'
    if CarryChain.at(source, 'HEAD').pending(CarryLedger(root).applied()):
        raise RuntimeError('Prepare output/wire with tools/upgrade_bus.py before this journey')
    home = output / 'home'
    home.mkdir(mode=0o700)
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith(('AGENT_COMMS_', 'PI_', 'PYTHON'))}
    environment.update(HOME=str(home), AGENT_COMMS_ROOT=str(root), VIRTUAL_ENV=str(old),
                       PI_CODING_AGENT_DIR=str(output / 'pi'),
                       AGENT_COMMS_AGENT_MODELS='openai-codex/gpt-6.1-sol')
    os.environ.update(environment)
    for key in tuple(os.environ):
        if key.startswith(('AGENT_COMMS_', 'PI_')) and key not in environment:
            del os.environ[key]
    current = StoppedOwnerInstallation.current_pointer()
    current.parent.mkdir(mode=0o700, parents=True)
    current.symlink_to(old)
    starter = '''
import sys
from pathlib import Path
from agent_comms.comms import Comms
from agent_comms.goals import Goal
from agent_comms.goal_states import BlockedGoal
c=Comms(Path(sys.argv[1]));c.messaging.initialize_private_initial_protocol()
c.registry.declare(c.registry.new_thread('publication-copy',frozenset({'copy'}),sys.argv[1],
 model='openai-codex/gpt-6.1-sol',goal=Goal('No input replay','copy-goal',published_seq=0,state=BlockedGoal('Verification only'))))
print(c.owners.start('publication-copy').pid,flush=True)
'''
    subprocess.run([old / 'bin/python', '-I', '-c', starter, str(root)], env=environment, check=True)
    os.environ['VIRTUAL_ENV'] = str(target)
    comms = Comms(root)
    original = comms.registry.require('publication-copy')
    deadline = time.monotonic() + 20
    while not socket_path(root, original.require_process().pid).exists():
        if not original.process_alive or time.monotonic() > deadline:
            raise RuntimeError('Isolated actual worker did not become ready')
        time.sleep(.02)
    marker = comms.bus.log.private_marker_unlocked()
    publish_active_route(ActiveRoute(root, marker.root_id))
    with comms.registry.store.editing() as edit:
        leased, _ = edit.lease_turn(edit.document.threads[original.name], 'controlled-publication-turn', None)
        edit.commit()
    lease = leased.turn_lease
    publication = RuntimePublication(target, {})
    results = {}
    try:
        # Kill only the isolated publisher while it is waiting, then re-enter
        # against the real maintenance witness it left behind.
        waiting = subprocess.Popen([target / 'bin/python', '-I', '-c', '''
import sys
from pathlib import Path
from tools.publish_runtime import RuntimePublication
from agent_comms.comms import Comms
root=Path(sys.argv[2]);c=Comms(root)
RuntimePublication(Path(sys.argv[3]),{}).publish(root,checkout=Path(sys.argv[1]))
''', str(source), str(root), str(target)], env=dict(os.environ))
        deadline = time.monotonic() + 10
        while not comms.owners.maintenance.read().turn_pending:
            assert waiting.poll() is None and time.monotonic() < deadline
            time.sleep(.01)
        waiting.kill()
        waiting.wait()
        assert current.resolve() == old
        try:
            publication.publish(root, checkout=source, timeout=.1)
            raise AssertionError('Busy owner did not prevent publication')
        except TimeoutError:
            results['timeout_released'] = comms.owners.maintenance.read().phase is ReadyPhase
            assert results['timeout_released'] and current.resolve() == old

        def during_hold(action):
            deadline = time.monotonic() + 10
            while not comms.owners.maintenance.read().turn_pending:
                assert time.monotonic() < deadline
                time.sleep(.01)
            time.sleep(.15)
            assert current.resolve() == old
            action()

        interrupt = threading.Thread(target=during_hold, args=(lambda: os.kill(os.getpid(), signal.SIGINT),))
        interrupt.start()
        try:
            publication.publish(root, checkout=source, timeout=10)
            raise AssertionError('Interrupt was not delivered')
        except KeyboardInterrupt:
            results['interrupt_released'] = comms.owners.maintenance.read().phase is ReadyPhase
            assert results['interrupt_released'] and current.resolve() == old
        interrupt.join()
        release = threading.Thread(target=during_hold, args=(lambda: comms.registry.release_turn(lease),))
        release.start()
        results['restarted'] = publication.publish(root, checkout=source, timeout=10)
        release.join()
        replacement = comms.registry.require(original.name)
        results.update(switched=current.resolve() == target,
                       maintenance_released=comms.owners.maintenance.read().phase is ReadyPhase,
                       original_exited=not original.process_alive,
                       replacement_live=replacement.process_alive,
                       goal_preserved=replacement.goal == original.goal,
                       new_pid=replacement.require_process().pid)
        assert all(results[key] for key in ('switched', 'maintenance_released', 'original_exited',
                                           'replacement_live', 'goal_preserved'))
        (output / 'result.json').write_text(json.dumps(results, indent=2) + '\n')
        print(json.dumps(results), flush=True)
    finally:
        comms.owners.stop(original.name)


if __name__ == '__main__':
    main()
