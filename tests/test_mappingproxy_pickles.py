"""A read-only view crosses a process boundary, as the observation pipe sends it."""

import pickle
from multiprocessing.reduction import ForkingPickler
from types import MappingProxyType

import agent_comms  # noqa: F401 - registers the view's pickler


def test_a_view_round_trips_as_a_view_through_the_pipe_pickler() -> None:
    view = MappingProxyType({"a": MappingProxyType({"b": (1, 2)})})
    received = pickle.loads(bytes(ForkingPickler.dumps(view)))
    assert type(received) is MappingProxyType and type(received["a"]) is MappingProxyType
    assert received == view
