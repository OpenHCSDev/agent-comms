"""A board prepares off-loop, publishes once, and follows store revisions without beats."""

import asyncio
import os
import threading
from contextlib import nullcontext
from dataclasses import replace
from multiprocessing import Pipe
from pathlib import Path

import pytest

from agent_comms.cli_bridges.codex_connection import CodexConnection
from agent_comms.coordination.service_passes import ServicePassStore
from agent_comms.execution.child_process import (
    NoProcess,
    ProcessIdentity,
)
from agent_comms.messaging.messages import InfoKind, Message
from agent_comms.messaging.revision_views import TeamBoardView
from agent_comms.planning.orders import (
    ProgressRead,
    TodoStore,
)
from agent_comms.presentation import WireRevision
from agent_comms.task_attachments.owner_notices import VibeCheckNotice
from agent_comms.task_attachments.team_pings import (
    AlarmSection,
    FoldedRule,
    RuleRow,
    TeamUpdateStore,
)
from agent_comms.team.standing_rules import OwedAnswer, StandingRule, TeamInputs
from agent_comms.team.team_heartbeat import (
    OwedAnswerCut,
    TeamBoardAcquisition,
    TeamHeartbeat,
    TeamObservation,
)
from agent_comms.team.team_hud import (
    AllTeamBoards,
    CodexUsage,
    NamedTeamBoards,
    TeamBoardPublicationStore,
    TeamHud,
    TeamPlan,
    UnreadableUsage,
)
from agent_comms.team.vibe_check import VibeCheckUnanswered
from agent_comms.ui_model.observation import (
    ObservationService,
    ObserveTeamHud,
    ObserveThreads,
    StopTeamHud,
    TeamHudObserved,
    TeamHudUnreadable,
    ThreadsObserved,
)
from test_holder_standing_rules import held_bus  # noqa: F401


async def receive(client, kind):
    """Decode the real observer pipe, rejecting a preparation error rather than timing out."""
    while True:
        if not await asyncio.to_thread(client.poll, 1):
            continue
        answer = client.recv()
        if isinstance(answer, TeamHudUnreadable):
            pytest.fail(str(answer.error))
        if isinstance(answer, kind):
            return answer


@pytest.fixture
def account_read(monkeypatch):
    """The account boundary keeps its real nested-loop requirement without a provider call."""
    async def usage():
        return CodexUsage(UnreadableUsage("test account"))

    monkeypatch.setattr(CodexConnection, "usage", lambda: asyncio.run(usage()))


def forbid_service_effect(*args, **kwargs):
    pytest.fail("Opening the board ran a heartbeat service effect")


def test_preparing_a_board_has_no_service_or_publication_effects(held_bus, account_read, monkeypatch):
    for owner, method in (
        (StandingRule, "enforce_all"), (TeamObservation, "merges"),
        (ProgressRead, "carry_reviews"), (TeamUpdateStore, "record"),
        (ServicePassStore, "heartbeat_passed"),
        (TeamBoardPublicationStore, "replace_all"),
    ):
        monkeypatch.setattr(owner, method, forbid_service_effect)
    before = held_bus.views.revision()
    acquisition = TeamBoardAcquisition()
    prepared = acquisition.acquire_board(held_bus)
    observed = acquisition.observe(held_bus, prepared)
    assert observed
    assert TeamBoardPublicationStore.read_all(held_bus.root) == ()
    assert not before.changed_since(TeamBoardView(), held_bus.views.revision())


def test_acquisition_and_observation_share_one_registry_cut(held_bus, monkeypatch):
    registry_type = type(held_bus.registry)
    snapshot = registry_type.snapshot
    captured = []
    direct_reads = []

    def counted(registry):
        cut = snapshot(registry)
        captured.append(cut)
        return cut

    def direct(method):
        def counted_direct(registry, *args, **kwargs):
            direct_reads.append(method.__name__)
            return method(registry, *args, **kwargs)

        return counted_direct

    monkeypatch.setattr(registry_type, "snapshot", counted)
    monkeypatch.setattr(registry_type, "require", direct(registry_type.require))
    monkeypatch.setattr(registry_type, "active_threads", direct(registry_type.active_threads))
    acquisition = TeamBoardAcquisition()
    prepared = acquisition.acquire_board(held_bus)
    observed = acquisition.observe(held_bus, prepared)

    assert len(captured) == 1
    assert direct_reads == []
    cut = captured[0]
    assert prepared and all(inputs.snapshot is cut for inputs in prepared)
    assert observed and all(board.frame.snapshot is cut for board in observed)


def test_todo_commit_signal_is_a_board_input_not_a_database_mtime(held_bus):
    paths = WireRevision.source_paths(held_bus.views)
    assert paths["team_board"] == TodoStore.source_paths(held_bus.root)
    assert TodoStore.path_in(held_bus.root) not in paths["team_board"]
    before = held_bus.views.revision()
    held_bus.goals.todos.add_request(
        held_bus.registry.require("colleague").incarnation, "Board input")
    assert before.changed_since(TeamBoardView(), held_bus.views.revision())


def test_frame_publication_and_expiry_do_not_request_another_preparation():
    before = WireRevision.empty()
    assert not replace(before, expiry_tick=60).changed_since(TeamBoardView(), before)


def test_steady_heartbeat_retains_its_board_derivations(held_bus, account_read, monkeypatch):
    """The sole writer reuses its exact publication and equal derived render owners."""
    colleague = held_bus.registry.require("colleague")
    held_bus.registry.register(
        replace(colleague, process_identity=ProcessIdentity.capture(os.getpid())))
    heartbeat = TeamHeartbeat()
    asyncio.run(heartbeat.run(lambda: nullcontext(held_bus)))
    retained = heartbeat._retained_board
    assert retained is not None
    before = {hud.teammate: hud for hud in retained.huds}
    for hud in before.values():
        _ = hud.alarms, hud.plain_group_text, hud.publication_facts
        for section in hud.alarms:
            _ = section.digest_source
            for folded in section.folded_rules:
                _ = folded.row_stems
                for row in folded.rows:
                    row.__dict__.pop("shown", None)

    monkeypatch.setattr(
        TeamBoardPublicationStore,
        "read",
        lambda self: pytest.fail("the board writer reread its own retained publication"),
    )
    monkeypatch.setattr(
        FoldedRule,
        "of",
        lambda *args, **kwargs: pytest.fail("an equal alarm rebuilt its fold shape"),
    )
    monkeypatch.setattr(
        AlarmSection,
        "by_kind",
        property(lambda self: pytest.fail("an equal alarm regrouped its digest")),
    )
    monkeypatch.setattr(
        RuleRow,
        "shown",
        property(lambda self: pytest.fail("an equal alarm rebuilt its static row")),
    )
    asyncio.run(heartbeat.run(lambda: nullcontext(held_bus)))

    current = heartbeat._retained_board
    assert current is not None
    after = {hud.teammate: hud for hud in current.huds}
    assert after.keys() == before.keys()
    for teammate, hud in after.items():
        assert [
            name for name, declared in hud.__dataclass_fields__.items()
            if declared.compare and getattr(hud, name) != getattr(before[teammate], name)
        ] == []
        assert hud.alarms is before[teammate].alarms
        assert hud.plain_group_text is before[teammate].plain_group_text
        assert hud.publication_facts is before[teammate].publication_facts

    rendered = next(hud for hud in after.values() if hud.open_pings)
    changed_ping = replace(rendered.open_pings[0], text="changed exact rendering source")
    changed = replace(
        rendered,
        open_pings=(changed_ping, *rendered.open_pings[1:]),
    )
    assert changed == rendered  # Occurrence evidence is deliberately outside ping equality.
    changed.retaining_rendering(rendered)
    assert changed.alarms is not rendered.alarms

    changed_huds = tuple(
        changed if hud.teammate == rendered.teammate else hud
        for hud in current.huds
    )
    published = TeamBoardPublicationStore.replace_all(
        held_bus.root,
        current.publication.plan,
        changed_huds,
        current,
    )
    assert published.publication != current.publication
    assert published.publication.changed == (rendered.teammate,)
    decoded = TeamBoardPublicationStore.at(held_bus.root).huds(
        publication=published.publication,
    )
    changed_frame = next(hud for hud in decoded if hud.teammate == rendered.teammate)
    assert changed_frame.open_pings[0].text == "changed exact rendering source"


def test_owed_answer_family_reuses_its_unchanged_source_cut(held_bus, monkeypatch):
    acquisition = TeamBoardAcquisition()
    snapshot = held_bus.registry.snapshot()
    project = snapshot.team_project.require()
    inputs = TeamInputs.prepare(held_bus, project, ProgressRead(), acquisition)
    owners = tuple(
        teammate.incarnation
        for teammate in acquisition.teammates(snapshot, held_bus.goals)
    )
    calls = 0
    read_scan = OwedAnswer.read_scan

    def counted(comms, owners, scan):
        nonlocal calls
        calls += 1
        return read_scan(comms, owners, scan)

    monkeypatch.setattr(OwedAnswer, "read_scan", counted)
    first = OwedAnswerCut.capture(
        held_bus, inputs, owners, held_bus.views.revision(), None,
    )
    second = OwedAnswerCut.capture(
        held_bus, inputs, owners, held_bus.views.revision(), first,
    )
    assert second.answers is first.answers
    assert calls == 1

    held_bus.goals.todos.add_request(
        held_bus.registry.require("colleague").incarnation,
        "A changed source invalidates the whole discovered answer family",
    )
    third = OwedAnswerCut.capture(
        held_bus, inputs, owners, held_bus.views.revision(), second,
    )
    assert calls == 2
    assert third.revision != second.revision


def test_owed_answer_cut_reopens_at_its_members_source_expiry(held_bus, monkeypatch):
    """A frozen store cannot keep an escalation after its owner says its window ended."""
    acquisition = TeamBoardAcquisition()
    snapshot = held_bus.registry.snapshot()
    project = snapshot.team_project.require()
    inputs = TeamInputs.prepare(held_bus, project, ProgressRead(), acquisition)
    owners = tuple(
        teammate.incarnation
        for teammate in acquisition.teammates(snapshot, held_bus.goals)
    )
    message = Message(
        "user", "colleague", "Answer the escalation", InfoKind,
        timestamp=100.0, seq=1,
    )
    answer = VibeCheckUnanswered(message)
    calls = 0

    def read_once_then_expired(comms, selected, scan):
        nonlocal calls
        calls += 1
        return (answer,) if calls == 1 else ()

    monkeypatch.setattr(OwedAnswer, "read_scan", read_once_then_expired)
    revision = held_bus.views.revision()
    boundary = message.timestamp + VibeCheckNotice.horizon
    first = OwedAnswerCut.capture(
        held_bus, inputs, owners, revision, None, now=boundary - 1,
    )
    retained = OwedAnswerCut.capture(
        held_bus, inputs, owners, revision, first, now=boundary - 0.5,
    )
    expired = OwedAnswerCut.capture(
        held_bus, inputs, owners, revision, retained, now=boundary,
    )

    assert first.valid_until == boundary
    assert retained.answers is first.answers
    assert expired.answers == ()
    assert calls == 2


def test_a_pass_that_throws_leaves_the_boards_it_could_not_replace(held_bus, account_read, monkeypatch):
    """A pass that raised holds a fragment, not the team, so it may not replace the frames.

    TeamBoardPublicationStore.replace_all unlinks every frame absent from the huds it is handed, which is
    exactly what a teammate that stopped beating deserves and the opposite of what a pass
    that threw deserves. On 2026-11-08 one Textual worktree could not read a blob for
    patch-id, so the first reconcile raised while huds was still empty, the finally retained
    that empty fragment, and every board on disk was deleted. Tristan's reads answered
    "Tristan frame missing" for eight hours, with the alarm about the failing pass having
    nowhere left to appear.
    """
    colleague = held_bus.registry.require("colleague")
    held_bus.registry.register(
        replace(colleague, process_identity=ProcessIdentity.capture(os.getpid())))
    heartbeat = TeamHeartbeat()
    asyncio.run(heartbeat.run(lambda: nullcontext(held_bus)))

    def on_disk() -> dict[Path, bytes]:
        """The files themselves, because the claim is that the pass may not replace them."""
        written = {path: path.read_bytes() for path in TeamBoardPublicationStore.matching_in(held_bus.root)}
        return written

    retained = on_disk()
    assert TeamBoardPublicationStore.matching_in(held_bus.root), "the fixture never gave Tristan a board to lose"

    def unreadable(self, comms):
        raise RuntimeError(
            "Git in /home/ts/wt/codex-textual-keys: could not fetch blob from promisor remote")

    monkeypatch.setattr(TeamObservation, "merges", unreadable)
    # The failure stays loud: the pass raises rather than swallowing, which is what records
    # it as a failing service pass. What it may not do is take the boards down with it.
    with pytest.raises(RuntimeError, match="promisor remote"):
        asyncio.run(heartbeat.run(lambda: nullcontext(held_bus)))

    # What the pass may not do is unlink a board or replace the plan. A board it finished
    # before the merge check threw is complete (pass_body builds every board before
    # reconcile reads git) and is kept, newer, by retention.keep; so frames are compared by
    # presence and the plan, which keep never writes, by its bytes.
    after = on_disk()
    assert after.keys() == retained.keys(), (
        "a failing merge check unlinked boards Tristan's reads still need"
    )

    # The other half, so the fix cannot degrade into never retaining at all: a pass that
    # gets through still replaces the frames. Checked by removing them and requiring the
    # next finished pass to put them back, which says nothing about who was beating. That a
    # finished pass also unlinks the frame of a teammate that stopped is replace_all's own
    # declared job (team_hud.py:374-376) and is not re-checked here.
    for path in TeamBoardPublicationStore.matching_in(held_bus.root):
        path.unlink()
    monkeypatch.setattr(TeamObservation, "merges", lambda self, comms: ())
    asyncio.run(heartbeat.run(lambda: nullcontext(held_bus)))
    assert TeamBoardPublicationStore.matching_in(held_bus.root), (
        "a finished pass wrote no frame, so this fix has degraded into never retaining"
    )
