"""Branch notices exercise real publication and retained registry identities."""
from pathlib import Path

import pytest

from agent_comms.comms import Comms
from agent_comms.projects.checkouts import Checkout
from agent_comms.registry.threads import NativeThread
from agent_comms.projects.repository_name import RepositoryName
from agent_comms.task_attachments.owner_notices import OrderNotice
from agent_comms.team.standing_rules import BranchBehindMain
from conftest import declare_test_bus


@pytest.fixture
def branch_notice(comms: Comms, tmp_path: Path) -> tuple[Comms, BranchBehindMain]:
    declare_test_bus(comms, channels=("comms",), threads=(
        NativeThread("colleague", frozenset({"comms"}), Checkout(tmp_path / "colleague")),
        NativeThread("worker", frozenset({"comms"}), Checkout(tmp_path / "worker")),
    ))
    return comms, BranchBehindMain(
        problem="P1", holder=comms.registry.require("worker").incarnation,
        repository=RepositoryName.from_wire("OpenHCSDev/agent-comms"),
        branch="feature/repair", behind=2,
    )


@pytest.mark.parametrize("rename_from,rename_to,target", (
    ("", "", "worker"),
    ("worker", "renamed-worker", "renamed-worker"),
    ("colleague", "renamed-colleague", "worker"),
))
def test_branch_notice_reaches_its_current_holder_in_cores_voice(
    branch_notice: tuple[Comms, BranchBehindMain], rename_from: str, rename_to: str, target: str,
) -> None:
    """No teammate signs Core's rebase reminder: it is an order notice to the holder's current
    name, whoever else is renamed."""
    comms, notice = branch_notice
    if rename_from:
        comms.registry.rename(rename_from, rename_to)
    before = comms.bus.bus.revision()

    notice.enforce(comms)

    assert comms.bus.bus.revision() == before + 1
    message = comms.bus.bus.delivery(before + 1).message
    assert message.target == target
    assert isinstance(message.task, OrderNotice)
    assert notice.branch in message.body
    assert "Rebase your branch onto origin/main" in message.body


@pytest.mark.parametrize("replacement", (False, True))
def test_branch_notice_never_addresses_a_departed_holders_replacement(
    branch_notice: tuple[Comms, BranchBehindMain], replacement: bool,
) -> None:
    comms, notice = branch_notice
    old = comms.registry.require("worker")
    comms.registry.unregister("worker")
    comms.registry.remove("worker")
    if replacement:
        comms.registry.declare(NativeThread(
            "worker", old.tags, old.worktree, created_at=old.created_at + 1))
    before = comms.bus.bus.revision()

    notice.enforce(comms)

    assert comms.bus.bus.revision() == before


def test_branch_notice_skips_an_archived_holder(branch_notice: tuple[Comms, BranchBehindMain]) -> None:
    """2026-11-02 15:04: this reminder to an archived holder raised 'Initial direct target must
    be visible' and failed every heartbeat pass, so no teammate had a board."""
    from agent_comms.registry.thread_status import ArchivedThreadStatus

    comms, notice = branch_notice
    comms.registry.register(comms.registry.require("worker"), ArchivedThreadStatus())
    before = comms.bus.bus.revision()

    notice.enforce(comms)

    assert comms.bus.bus.revision() == before
