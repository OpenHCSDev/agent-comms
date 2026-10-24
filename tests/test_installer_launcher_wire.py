"""The launcher wire keeps the phase records deployed runtimes already send."""

from pathlib import Path

from agent_comms.field_codec import FieldCodec
from agent_comms.installer.interpreter_operation import (
    GitPhase,
    LaunchedPhaseTiming,
    PhaseTiming,
)

LAUNCHER_PHASES = [
    {"phase": {"kind": "launcher", "detail": ""}, "seconds": 1.0, "elapsed_seconds": 1.0},
    {
        "phase": {"kind": "git", "detail": "git -C /x fetch -q origin"},
        "seconds": 1.0,
        "elapsed_seconds": 1.0,
    },
    {
        "phase": {"kind": "prepare", "detail": "target checkout"},
        "seconds": 1.0,
        "elapsed_seconds": 1.0,
    },
]


def test_deployed_launcher_timings_keep_their_labels():
    timings = FieldCodec.decode(list[LaunchedPhaseTiming], LAUNCHER_PHASES)

    assert [timing.timing().phase.label() for timing in timings] == [
        "LauncherPhase",
        "GitPhase: git -C /x fetch -q origin",
        "PreparePhase: target checkout",
    ]


def test_typed_git_phase_encodes_the_deployed_launcher_wire():
    timing = PhaseTiming(GitPhase(Path("/x"), ("fetch", "-q", "origin")), 1.0, 1.0)

    assert FieldCodec.encode(timing.launched()) == LAUNCHER_PHASES[1]
