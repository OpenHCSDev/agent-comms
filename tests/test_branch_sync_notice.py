"""Branch notices exercise real publication and retained registry identities."""
from pathlib import Path

import pytest

from agent_comms.comms import Comms
from agent_comms.repo import Checkout
from agent_comms.repository_name import RepositoryName
from agent_comms.standing_rules import BranchBehindMain
from agent_comms.threads import Thread
from conftest import declare_test_bus


@pytest.fixture
def branch_notice(comms: Comms, tmp_path: Path) -> tuple[Comms, BranchBehindMain]:
    declare_test_bus(comms, channels=("comms",), threads=(
        Thread("lead", frozenset({"comms"}), Checkout(tmp_path / "lead")),
        Thread("worker", frozenset({"comms"}), Checkout(tmp_path / "worker")),
    ))
    return comms, BranchBehindMain(
        problem="P1", holder=comms.registry.require("worker").incarnation,
        repository=RepositoryName.from_wire("OpenHCSDev/agent-comms"),
        branch="feature/repair", behind=2,
    )


@pytest.mark.parametrize("sender,rename_from,rename_to,target,actor", (
    ("lead", "", "", "worker", "lead"),
    ("worker", "", "", "#comms", "worker"),
    ("lead", "worker", "renamed-worker", "renamed-worker", "lead"),
    ("lead", "lead", "renamed-lead", "worker", "renamed-lead"),
    ("worker", "worker", "renamed-worker", "#comms", "renamed-worker"),
))
def test_branch_notice_reaches_its_current_audience(
    branch_notice: tuple[Comms, BranchBehindMain], sender: str,
    rename_from: str, rename_to: str, target: str, actor: str,
) -> None:
    comms, notice = branch_notice
    if rename_from:
        comms.registry.rename(rename_from, rename_to)
    before = comms.bus.bus.revision()

    notice.enforce(comms, sender)

    assert comms.bus.bus.revision() == before + 1
    message = comms.bus.bus.delivery(before + 1).message
    assert message.target == target
    assert message.sender == actor
    assert notice.branch in message.body
    assert "Rebase your branch onto origin/main" in message.body
    assert "--force-with-lease" in message.body
    assert "REBASE" in notice.line()


@pytest.mark.parametrize("replacement", (False, True))
def test_branch_notice_never_addresses_a_departed_holders_replacement(
    branch_notice: tuple[Comms, BranchBehindMain], replacement: bool,
) -> None:
    comms, notice = branch_notice
    old = comms.registry.require("worker")
    comms.registry.unregister("worker")
    comms.registry.remove("worker")
    if replacement:
        comms.registry.declare(Thread(
            "worker", old.tags, old.worktree, created_at=old.created_at + 1,
        ))
    before = comms.bus.bus.revision()

    notice.enforce(comms, "lead")

    assert comms.bus.bus.revision() == before


def test_branch_notice_skips_an_archived_holder(branch_notice: tuple[Comms, BranchBehindMain]) -> None:
    """2026-11-02 15:04: this reminder to an archived holder raised 'Initial direct target must
    be visible' and failed every heartbeat pass, so no teammate had a board."""
    from agent_comms.thread_status import ArchivedThreadStatus

    comms, notice = branch_notice
    comms.registry.register(comms.registry.require("worker"), ArchivedThreadStatus())
    before = comms.bus.bus.revision()

    notice.enforce(comms, "lead")

    assert comms.bus.bus.revision() == before
