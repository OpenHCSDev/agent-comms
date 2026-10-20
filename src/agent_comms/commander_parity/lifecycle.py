"""Live order-family observations; partial observations never authorize a switch."""

from __future__ import annotations

import sys
from types import FrameType

from ..commander.config import CommanderConfig
from ..commander.order import Commander
from ..todos import Moving, NoCommitProgress, OrderProgress, Todo
from . import ParityComparison, UnverifiedParity


def verify_order_progress() -> ParityComparison:
    """Observe the real native measurements returned through the current reader.

    This first checkpoint deliberately reports incomplete coverage: matching a
    read is not proof of scheduling, strike suppression or unit-facing delivery.
    """
    context = Commander.read(CommanderConfig.path())
    observed: dict[str, str] = {}

    def observe(frame: FrameType, event: str, value: object) -> None:
        if event == "return" and frame.f_code is OrderProgress.read.__func__.__code__:
            todo = frame.f_locals["todo"]
            if not isinstance(todo, Todo) or not isinstance(value, OrderProgress):
                raise TypeError("The native progress reader returned an unexpected record")
            observed[todo.id] = value.line

    previous = sys.getprofile()
    sys.setprofile(observe)
    try:
        current = {key: value.line for key, value in context.progress().items()}
    finally:
        sys.setprofile(previous)
    return UnverifiedParity(
        f"Live OrderProgress.read: {len(observed)} native observations, "
        f"current/native equal={bool(observed) and current == observed}; "
        f"first-commit interval={NoCommitProgress.seconds}s, "
        f"later-progress interval={Moving.seconds}s. "
        "Scheduling, merged-work strike suppression and unit deadline visibility "
        "still require their live workflow comparison."
    )
