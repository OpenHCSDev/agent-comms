"""A registry revision names the threads it changed, so an observer re-reads only those."""

import subprocess
import sys
from dataclasses import replace
from pathlib import Path

from agent_comms.projects.checkouts import Checkout
from agent_comms.registry.registration import Registration
from agent_comms.registry.registry_document import UnreadRegistry
from agent_comms.registry.thread_text import (
    ForkedParent,
    GivenThreadText,
)
from agent_comms.registry.threads import NativeThread
from agent_comms.storage.locked_store import RegistryFile


def registry_with_a_fork(root: Path) -> Registration:
    registry = Registration(RegistryFile.path_in(root))
    registry.register(NativeThread("alpha", frozenset(), Checkout(root), created_at=10.0))
    registry.register(NativeThread("beta", frozenset(), Checkout(root), created_at=20.0))
    registry.register(NativeThread("alpha-fork", frozenset(), Checkout(root),
                                   parent=ForkedParent("alpha"), created_at=30.0))
    return registry


def test_a_write_to_one_thread_names_that_thread_and_its_fork_only(tmp_path: Path) -> None:
    """Every turn start rewrites the registry; it used to re-present every open thread."""
    registry = registry_with_a_fork(tmp_path)
    before = registry.snapshot()
    registry.register(replace(registry.require("alpha"), title=GivenThreadText("renamed")))
    after = registry.snapshot()

    changed = after.changed_names(before)

    assert changed == {"alpha"}
    assert after.affected_by("alpha", changed)
    assert after.affected_by("alpha-fork", changed)
    assert not after.affected_by("beta", changed)


def test_an_unchanged_registry_names_nothing(tmp_path: Path) -> None:
    registry = registry_with_a_fork(tmp_path)
    assert registry.snapshot().changed_names(registry.snapshot()) == frozenset()


def test_a_reader_that_read_no_cut_rereads_every_thread(tmp_path: Path) -> None:
    snapshot = registry_with_a_fork(tmp_path).snapshot()
    assert UnreadRegistry().changed_names_to(snapshot) >= {"alpha", "beta", "alpha-fork"}


def test_a_slice_holds_what_the_named_presentation_reads(tmp_path: Path) -> None:
    """A remote UI opening a fork reads its row and its parent's, never the other threads'."""
    snapshot = registry_with_a_fork(tmp_path).snapshot()

    cut = snapshot.only(("alpha-fork",))

    assert set(cut.threads) == {"alpha-fork", "alpha"}
    assert "beta" not in cut and "beta" not in cut.statuses
    assert cut.require("alpha-fork").incarnation == snapshot.require("alpha-fork").incarnation
    assert cut.status("alpha") == snapshot.status("alpha")
    assert cut.team_project == snapshot.team_project


def test_a_name_outside_the_slice_says_so(tmp_path: Path) -> None:
    import pytest

    from agent_comms.errors import OutsideRegistrySlice, UnregisteredThreadError

    cut = registry_with_a_fork(tmp_path).snapshot().only(("alpha",))
    with pytest.raises(OutsideRegistrySlice, match="not in this registry slice") as refused:
        cut.require("beta")
    # beta is registered; a handler for gone threads must not read this as one.
    assert not isinstance(refused.value, UnregisteredThreadError)


def test_a_name_asked_for_but_never_registered_is_unregistered(tmp_path: Path) -> None:
    import pytest

    from agent_comms.errors import OutsideRegistrySlice, UnregisteredThreadError

    cut = registry_with_a_fork(tmp_path).snapshot().only(("alpha", "nobody"))
    with pytest.raises(UnregisteredThreadError, match="is not registered") as refused:
        cut.require("nobody")
    assert not isinstance(refused.value, OutsideRegistrySlice)


def test_a_slice_of_a_slice_keeps_what_both_asked_for(tmp_path: Path) -> None:
    """Slicing a slice again narrows it; its own ``asked`` is not passed twice."""
    snapshot = registry_with_a_fork(tmp_path).snapshot()
    narrower = snapshot.only(("alpha-fork", "beta")).only(("alpha-fork",))
    assert set(narrower.threads) == {"alpha-fork", "alpha"}
    assert narrower.asked == frozenset({"alpha-fork"})


WRITE_TITLE = """
import sys
from dataclasses import replace
from pathlib import Path
from agent_comms.registry.registration import Registration
from agent_comms.registry.thread_text import GivenThreadText
registry = Registration(Path(sys.argv[1]))
registry.register(replace(registry.require(sys.argv[2]), title=GivenThreadText(sys.argv[3])))
"""


def another_process_retitles(path: Path, name: str, title: str) -> None:
    subprocess.run([sys.executable, "-c", WRITE_TITLE, str(path), name, title], check=True)


def written_by_another_process(root: Path) -> Path:
    """The registry with a fork, none of it decoded in this process yet."""
    subprocess.run([sys.executable, "-c", (
        "import sys; from pathlib import Path; sys.path.insert(0, sys.argv[2]);"
        "from test_registry_changed_names import registry_with_a_fork;"
        "registry_with_a_fork(Path(sys.argv[1]))"), str(root), str(Path(__file__).parent)],
        check=True)
    return RegistryFile.path_in(root)


def test_a_reader_process_decodes_each_unchanged_row_once(tmp_path: Path) -> None:
    """The observer and connector only read the registry file, so every row they hold arrives
    raw and is decoded through a frozen snapshot. Those decodes reach the revision the snapshot
    was frozen from, so another process's write re-decodes only the rows it changed: on
    2026-11-04 every heartbeat revision re-decoded all 2,911 rows, about 1 s per walk."""
    path = written_by_another_process(tmp_path)
    reader = Registration(path)
    before = reader.snapshot()
    read = {name: before.threads[name] for name in before.threads}
    another_process_retitles(path, "alpha", "renamed")
    after = reader.snapshot()

    assert after.threads["beta"] is read["beta"]
    assert after.threads["alpha-fork"] is read["alpha-fork"]
    assert after.threads["alpha"].title == GivenThreadText("renamed")
    renamed = after.threads["alpha"]
    another_process_retitles(path, "beta", "moved")
    assert reader.snapshot().threads["alpha"] is renamed


def test_every_reader_of_one_registry_keeps_the_rows_another_decoded(tmp_path: Path) -> None:
    """A turn builds its own registry reader, and a row is bound to the registry file it came
    from. Bound to the reader that asked instead, a row another reader decoded was dropped by
    the next one: on 2026-11-14 the worker decoded every thread it touched again after each
    registry write, about 1 ms a thread, several writes a turn."""
    path = written_by_another_process(tmp_path)
    before = Registration(path).snapshot()
    read = {name: before.threads[name] for name in before.threads}
    another_process_retitles(path, "alpha", "renamed")
    after = Registration(path).snapshot()

    assert after.threads["beta"] is read["beta"]
    assert after.threads["alpha-fork"] is read["alpha-fork"]
    assert after.threads["alpha"].title == GivenThreadText("renamed")
