"""Runtime contracts of the original live and retained revision owners."""

from dataclasses import replace
from inspect import isabstract
from typing import get_args, get_origin, get_type_hints

import pytest

from agent_comms.history_views import HistoryViews
from agent_comms.messaging.message_bus import MessageBus
from agent_comms.messaging.published_bus import PublishedBus
from agent_comms.planning.goal_management import Goals
from agent_comms.planning.goal_presentation import GoalExecutionControls, GoalSnapshot
from agent_comms.presentation import (
    BusPresentation,
    CoordinationSnapshot,
    InboxRead,
    ThreadRecord,
    ThreadView,
    WireRevision,
    expiry_period_changed,
    revision_source,
)
from agent_comms.registry.agent_activity import ActivityAnswers
from agent_comms.registry.registry_document import RegistrySnapshot
from agent_comms.registry.threads import Thread
from agent_comms.ui_model.observation import ThreadsObserved


def test_revision_owner_callbacks_resolve() -> None:
    assert get_type_hints(HistoryViews.revision)["return"] is WireRevision
    assert get_type_hints(ThreadsObserved.changed_threads)["revision"] is WireRevision
    paths = WireRevision.source_paths
    assert get_type_hints(paths)["views"] is paths.__type_params__[0]
    get_type_hints(revision_source)
    get_type_hints(WireRevision.period_for)


@pytest.mark.parametrize("before,now", [(0, 29), (29, 30), (30, 59), (59, 60), (-1, 0)])
def test_revision_period_uses_its_owned_projection(before: int, now: int) -> None:
    original = WireRevision.empty()
    before_period = replace(original, expiry_tick=before).expiry_period
    now_period = replace(original, expiry_tick=now).expiry_period
    assert WireRevision.period_for(before) == before_period
    assert WireRevision.period_for(now) == now_period
    assert expiry_period_changed(before, now) == (before_period != now_period)


def test_presentation_sources_are_actual_nominal_providers() -> None:
    bus_source = get_type_hints(BusPresentation.__init__)["bus"]
    goal_alias = get_type_hints(BusPresentation.__init__)["goals"]
    goal_source = get_origin(goal_alias)
    owner, snapshot_contract, registry_contract, activity_contract = get_args(goal_alias)
    assert bus_source in MessageBus.__bases__
    assert goal_source in Goals.__bases__
    assert not isabstract(MessageBus)
    assert not isabstract(Goals)
    assert bus_source.opened_activity is PublishedBus.opened_activity
    assert owner is Thread
    assert registry_contract is RegistrySnapshot
    assert activity_contract is ActivityAnswers
    assert snapshot_contract in GoalSnapshot.__bases__
    assert get_type_hints(GoalSnapshot)["execution"] is GoalExecutionControls
    owning_alias, = (base for base in Goals.__orig_bases__
                      if get_origin(base) is goal_source)
    assert get_args(owning_alias) == (Thread, GoalSnapshot, RegistrySnapshot, ActivityAnswers)
    owner_parameter, snapshot_parameter, registry_parameter, activity_parameter = goal_source.__type_params__
    assert get_type_hints(goal_source.snapshot_for)["snapshot"] is registry_parameter
    assert get_type_hints(goal_source.snapshot_for)["owner"] is owner_parameter
    assert get_type_hints(goal_source.snapshot_for)["return"] is snapshot_parameter
    assert get_type_hints(goal_source.snapshots_for)["activity"] is activity_parameter
    for member in InboxRead.members_with(InboxRead):
        get_type_hints(member.counts)
    for callback in (InboxRead.counts, ThreadRecord.read, ThreadView.roster,
                     CoordinationSnapshot.capture):
        get_type_hints(callback)
