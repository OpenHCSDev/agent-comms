"""The source acquisition graph must respect every declaration-owned store tier."""

from importlib import import_module
from inspect import getfile
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
    failures = []
    checked = 0
    borrowed = 0
    for outer, inner, reentry in sorted(graph.pairs):
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


def test_new_lock_method_and_wrapper_are_discovered(tmp_path):
    source = tmp_path / "src"
    source.mkdir()
    (source / "store_files.py").write_text(Path(getfile(StoreLockOwner)).read_text())
    (source / "new_methods.py").write_text("""
from contextlib import contextmanager
from agent_comms.store_files import CatalogTier, RegistryTier

class NewRegistry(RegistryTier):
    @contextmanager
    def invented_guard(self):
        with self.locked():
            yield

    @staticmethod
    def invented_factory(action):
        def invented_closure(self):
            with self.invented_guard():
                return action(self)
        return invented_closure

    run_action = invented_factory(lambda self: None)

class NewCatalog(CatalogTier):
    def invert(self, registry: NewRegistry):
        with self.locked():
            registry.run_action()
""")
    graph = StoreLockGraph(tmp_path)
    inversions = [
        (outer, inner)
        for outer, inner, reentry in graph.pairs
        if not reentry and graph.ranks[outer.store] > graph.ranks[inner.store]
    ]
    assert inversions, "A new lock method and returned wrapper must expose their inversion"
