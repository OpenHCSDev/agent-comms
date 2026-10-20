import json
from dataclasses import replace

from agent_comms.comms import Comms
from agent_comms.commander.config import CommanderConfig
from cutover.carry import CarryLedger
from cutover.carry_parked_threads import ParkedThreadsCarry


def test_legacy_parking_carries_through_the_registry_writer(commander, commander_script, monkeypatch):
    comms, _ = commander
    config = CommanderConfig.root_path(comms.root)
    raw = json.loads(config.read_text())
    raw['parked'] = ['worker']
    config.write_text(json.dumps(raw))
    before = comms.registry.require('worker')
    def forbidden_home_config(*args):
        raise AssertionError('A copied bus must not read the current machine home config.')
    monkeypatch.setattr(CommanderConfig, 'path', staticmethod(forbidden_home_config))
    dry = ParkedThreadsCarry.carry(comms.root, apply=False)
    assert dry.changed == 1 and not dry.rewritten
    assert not comms.registry.require('worker').parked
    carried = ParkedThreadsCarry.carry(comms.root, apply=True)
    assert carried.changed == 1 and carried.rewritten == ('registry.json',)
    fresh = Comms(comms.root)
    assert fresh.registry.require('worker') == replace(before, parked=True)
    assert ParkedThreadsCarry.carry(comms.root, apply=True).changed == 0
    # Registration from an older owner snapshot cannot reset the registry-owned flag.
    fresh.registry.register(before, fresh.registry.status('worker'))
    assert comms.registry.require('worker').parked
    fresh.registry.set_parked('worker', False)
    assert not comms.registry.require('worker').parked
