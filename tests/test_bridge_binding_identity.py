"""An enrolled address becomes a registered reader without changing its journal."""
from dataclasses import replace

import pytest

from agent_comms.cli_bridges.claude import ClaudeBridge
from agent_comms.cli_bridges.shared import (
    Binding,
    BindingAddress,
    CommandedBy,
    Enrol,
    Options,
    StoredBinding,
    Watch,
)
from agent_comms.threads import Thread
from agent_comms_hooks.watch import WatchClaim


@pytest.mark.asyncio
@pytest.mark.parametrize('operation,tags', [
    (Watch(CommandedBy('commander')), frozenset({'comms'})),
    (Watch(), frozenset()),
])
async def test_enrol_register_rename_and_rebind_preserve_reader(comms, tmp_path, operation, tags):
    comms.registry.declare(Thread('commander', frozenset({'comms'}), str(tmp_path)))
    state = tmp_path / 'journal'
    address = BindingAddress(root=str(comms.root), bus='new-reader', thread='session',
                             start=Binding.bus_head(str(comms.root)))
    with Binding.open(state) as db:
        bridge = ClaudeBridge(Options(Enrol(), StoredBinding(), state, state.parent), address, db)
        bridge.binding = await bridge.options.operation.prepare(bridge)
        await bridge.options.operation.execute(bridge)
        assert WatchClaim('session').state().bindings()[0].bus == 'new-reader'
        assert not comms.registry.snapshot().taken('new-reader')
        assert Binding.select(db) == []

        bridge.binding = await operation.prepare(bridge)
        original, = Binding.select(db)
        reader = comms.registry.require('new-reader')
        assert original.owner == reader.incarnation
        assert reader.tags == tags

        comms.registry.rename('new-reader', 'renamed-reader')
        bridge.binding = await operation.prepare(bridge)
        renamed, = Binding.select(db)
        assert renamed.bus == 'renamed-reader'
        assert renamed.same_reader(original)
        assert renamed.start == original.start

        # A reused address is not the old reader, even in the same bus root.
        comms.registry.unregister('renamed-reader')
        comms.registry.remove('renamed-reader')
        comms.registry.declare(Thread('renamed-reader', frozenset(), str(tmp_path)))
        replacement = replace(address, bus='renamed-reader', thread='other-session')
        bridge.binding = replacement
        with pytest.raises(ValueError, match='another Core/CLI conversation'):
            await operation.prepare(bridge)
        assert Binding.select(db) == [renamed]
