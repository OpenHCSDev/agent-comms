"""Target families never disguise a broken binding as an unavailable command."""

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from agent_comms.cli_verbs.archive import ArchiveCliCommand
from agent_comms.cli_verbs.cli_command import (
    CliCommand,
    OneTarget,
    TargetActionCommand,
    TargetBatch,
    TargetBatchResult,
    TargetCompleted,
    TargetFailed,
)
from agent_comms.cli_verbs.cli_commands import ThreadStoppedResult
from agent_comms.cli_verbs.cli_result import ResultShower
from agent_comms.cli_verbs.new import NewThreadCliCommand
from agent_comms.cli_verbs.queue_restart import QueueRestartCliCommand
from agent_comms.cli_verbs.restart import RestartCliCommand
from agent_comms.cli_verbs.stop import StopCliCommand
from agent_comms.messaging.channel_targets import (
    CommandTarget,
    IncarnationThreadTarget,
    ProjectTarget,
)
from agent_comms.nominal.field_codec import FieldCodec
from agent_comms.projects.checkouts import Checkout
from agent_comms.registry.thread_identity import RecordedThreadIncarnation
from agent_comms.registry.thread_status import (
    ArchivedThreadStatus,
    RunningThreadStatus,
    StoppedThreadStatus,
    ThreadStatus,
)
from agent_comms.registry.threads import NativeThread


@dataclass
class RecordingShower(ResultShower):
    done: list[tuple[str, str]] = field(default_factory=list)
    errors: list[tuple[Exception | str, str]] = field(default_factory=list)

    def show_done(self, text: str, title: str) -> None:
        self.done.append((text, title))

    def show_error(self, error: Exception | str, title: str) -> None:
        self.errors.append((error, title))


@pytest.mark.parametrize("target", [
    CommandTarget.named("first"), CommandTarget.named("#team"), ProjectTarget(Checkout(Path("/project"))),
    IncarnationThreadTarget(RecordedThreadIncarnation("first", 1.0)),
])
def test_batch_completed_result_round_trips_with_its_command_result_type(target):
    original = TargetBatchResult((
        TargetCompleted(target, StopCliCommand(name="first"), ThreadStoppedResult("first")),
        TargetFailed(target, "UnregisteredThreadError", "Thread 'missing' is not registered."),
    ))
    decoded = FieldCodec.decode(TargetBatchResult, FieldCodec.encode(original))
    assert decoded == original
    assert decoded.outcomes[0].owner_start().name == target.display_name()
    assert decoded.outcomes[1].owner_start().name == target.display_name()
    assert str(decoded.outcomes[1]).startswith(target.display_name())
    shower = RecordingShower()
    decoded.show(shower, "Stop", "Action")
    assert shower.done == [(f"Stop: {target.display_name()}", "Action")]
    assert shower.errors == [(str(decoded.outcomes[1]), "Action")]


@pytest.fixture
def running_first(comms, tmp_path):
    comms.registry.declare(NativeThread("first", frozenset(), Checkout(tmp_path)), RunningThreadStatus())
    return comms


def test_one_target_stops_a_registered_thread_and_refuses_an_unknown_one(running_first, tmp_path):
    comms = running_first
    with pytest.raises(ValueError):
        StopCliCommand.execute_target(comms, OneTarget((CommandTarget.named("ghost"),)), {},
                                      project=str(tmp_path))
    assert not comms.registry.status("first").stopped
    result = StopCliCommand.execute_target(comms, OneTarget((CommandTarget.named("first"),)), {},
                                           project=str(tmp_path))
    assert result.stopped == "first"
    assert comms.registry.status("first").stopped


def test_target_batch_reports_an_unknown_target_as_a_failed_outcome(running_first, tmp_path):
    comms = running_first
    missed = StopCliCommand.execute_target(comms, TargetBatch((CommandTarget.named("ghost"),)), {},
                                           project=str(tmp_path))
    assert type(missed) is TargetBatchResult and not missed.successful
    assert type(missed.outcomes[0]) is TargetFailed
    assert not comms.registry.status("first").stopped
    stopped = StopCliCommand.execute_target(comms, TargetBatch((CommandTarget.named("first"),)), {},
                                            project=str(tmp_path))
    assert stopped.successful and stopped.outcomes[0].result.stopped == "first"
    assert comms.registry.status("first").stopped


def test_batch_capability_owns_selection_catalog_and_execution(comms, tmp_path):
    names = ("first", "second")
    for name in names:
        comms.registry.declare(NativeThread(name, frozenset(), Checkout(tmp_path)),
                               StoppedThreadStatus())
    selected = CliCommand.selected_targets(tuple(CommandTarget.named(name) for name in names))
    catalog = CliCommand.target_catalog(comms, selected, project=Checkout(tmp_path))
    assert any(action.declaration is ArchiveCliCommand for action in catalog)
    assert all(action.declaration is not QueueRestartCliCommand for action in catalog)
    with pytest.raises(ValueError, match="single target"):
        RestartCliCommand.execute_target(comms, RestartCliCommand.selected_targets(tuple(CommandTarget.named(name) for name in names)), {}, project=str(tmp_path))
    assert all(comms.registry.status(name) == StoppedThreadStatus() for name in names)
    result = ArchiveCliCommand.execute_target(
        comms, ArchiveCliCommand.selected_targets(tuple(CommandTarget.named(name) for name in names)), {}, confirmed=True, project=str(tmp_path))
    assert len(result.outcomes) == 2 and result.exit_code() == 0
    assert tuple(outcome.target for outcome in result.outcomes) == tuple(
        CommandTarget.named(name) for name in names)
    assert all(comms.registry.status(name) == ArchivedThreadStatus() for name in names)


@pytest.mark.parametrize("presence,allowed", [
    ("running", True), ("idle", True), ("stopped", True),
    ("archived", False), ("deleting", False),
])
def test_channel_pin_filters_hidden_thread_presence(comms, tmp_path, presence, allowed):
    comms.registry.declare(
        NativeThread("first", frozenset({"team"}), Checkout(tmp_path)),
        ThreadStatus.member_named(presence)(),
    )
    if allowed:
        comms.channels.set_thread_pinned("#team", "first", True)
        assert "first" in comms.channels.catalog.read().pinned_members().get("#team", frozenset())
    else:
        with pytest.raises(ValueError, match="not a member"):
            comms.channels.set_thread_pinned("#team", "first", True)
        assert "first" not in comms.channels.catalog.read().pinned_members().get("#team", frozenset())


def test_a_project_menu_offers_a_new_thread(comms, tmp_path):
    """Toad's "+ New Session" opens the project's new-thread action; the catalog lists it."""
    project = Checkout(tmp_path)
    actions = TargetActionCommand.target_catalog(
        comms, TargetActionCommand.selected_targets((ProjectTarget(project),)), project=project)
    assert [action.declaration for action in actions] == [NewThreadCliCommand]
