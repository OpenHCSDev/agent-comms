"""The source acquisition graph must respect every declaration-owned store tier."""

from importlib import import_module
from pathlib import Path

import pytest

from agent_comms.store_files import CatalogTier, RegistryTier, StoreLockOwner
from store_lock_graph import StoreLockGraph


def test_source_acquisitions_follow_store_tiers():
    graph = StoreLockGraph(Path(__file__).resolve().parents[1])
    # Bind source declarations to their actual classes; no store roster or copied ranks.
    owners = {}
    for name in graph.ranks:
        module, declaration = graph.declaration(name)
        imported = import_module(
            module.path.removeprefix("src/").removesuffix(".py").replace("/", ".")
        )
        owners[name] = vars(imported)[declaration.name]
    StoreLockOwner.require_ranked()
    failures = []
    checked = 0
    borrowed = 0
    for outer, inner, reentry in sorted(graph.pairs):
        # RestartQueue's physical lock is outside the StoreLockOwner family.
        if outer.store not in owners or inner.store not in owners:
            continue
        first, second = owners[outer.store].lock_rank(), owners[inner.store].lock_rank()
        valid = first == second if reentry else first < second
        checked += 1
        borrowed += reentry
        line = (
            f"{outer.store}({first}) -> {inner.store}({second}) "
            f"{outer.via} -> {inner.via}; locks {outer.site} -> {inner.site}"
        )
        print(line + (" [reentry]" if reentry else ""))
        if not valid:
            failures.append(line)
    assert checked, "The acquisition census found no store pairs"
    assert not failures, "Store lock acquisition inversions:\n" + "\n".join(failures)
    # Missing and conflicting tiers fail when a class registers, before it takes a lock.
    with pytest.raises(TypeError, match="MissingTier must inherit exactly one"):

        class MissingTier(StoreLockOwner):
            pass

    with pytest.raises(TypeError, match="ConflictingTiers must inherit exactly one"):

        class ConflictingTiers(RegistryTier, CatalogTier):
            pass

    print(f"{checked} store acquisition pairs checked; {borrowed} borrowed-lock reentries")
