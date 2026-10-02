"""Run the affected USER journeys with installed declarations, no source overlay.

Reuse the continuous controls instead of copying their publisher/selection
assertions. No conftest scheduler replacement or model/native launch is used.
"""
import hashlib
import importlib.util
import json
from pathlib import Path
import sys

import agent_comms
from agent_comms.comms import Comms


def main():
    stage = Path(sys.argv[1]).absolute()
    stage.mkdir(mode=0o700, parents=True, exist_ok=False)
    checkout = Path(__file__).resolve().parents[2]
    installed = Path(agent_comms.__file__).resolve().parent
    if checkout in installed.parents:
        raise ValueError('The affected journey requires an installed wheel, not source overlay.')
    spec = importlib.util.spec_from_file_location(
        'user_task_controls', checkout / 'tests/test_task_decisions.py')
    controls = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(controls)
    names = (
        'test_user_pin_original_source_without_model_lease_and_complete_lineage',
        'test_user_pin_goal_scope_replacement_and_original_subject_fences',
    )
    results = []
    for name in names:
        service = Comms(stage / name)
        getattr(controls, name)(service)
        snapshot = service.registry.snapshot()
        assert all(not owner.pid and owner.turn_lease is None
                   for owner in snapshot.threads.values())
        results.append({'journey': name, 'state': 'PASS',
                        'wire_rows': len(service.bus.log.full_history())})
    modules = ('task_sources.py', 'messaging.py', 'messages.py', 'retained_task_facts.py')
    hashes = {}
    for name in modules:
        actual = (installed / name).read_bytes()
        assert actual == (checkout / 'src/agent_comms' / name).read_bytes()
        hashes[name] = hashlib.sha256(actual).hexdigest()
    receipt = {'state': 'INSTALLED_USER_PUBLISHER_SOURCE_JOURNEYS_PASS',
               'installed_package': str(installed), 'module_sha256': hashes,
               'journeys': results, 'provider_calls': 0, 'input_replays': 0,
               'native_launches': 0, 'conftest_scheduler_mock': False,
               'public_changes': [], 'full_native_ACP_UI_acceptance': False}
    (stage / 'receipt.json').write_text(json.dumps(receipt, indent=2) + '\n')
    print(json.dumps(receipt), flush=True)


if __name__ == '__main__':
    main()
