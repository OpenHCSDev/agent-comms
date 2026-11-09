"""Runtime contracts of the original live and retained revision owners."""

from dataclasses import replace
from typing import get_type_hints

import pytest

from agent_comms.history_views import HistoryViews
from agent_comms.presentation import WireRevision, expiry_period_changed, revision_source
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
