"""Goal publication callbacks remain inspectable by runtime consumers."""

from collections.abc import Callable
from typing import get_type_hints

import pytest

from agent_comms.context_segments.wake_values import WakeWorkContext
from agent_comms.goals import GoalUnavailable
from agent_comms.registry.threads import Thread


@pytest.mark.parametrize(
    ("callback", "result"),
    ((GoalUnavailable.heading, str), (Thread.wake_context, WakeWorkContext)),
)
def test_goal_publication_callback_annotations_resolve(
    callback: Callable[..., object], result: type,
) -> None:
    assert get_type_hints(callback)["return"] is result
