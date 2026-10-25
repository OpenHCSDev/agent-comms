"""The launcher wire keeps the phase records deployed runtimes already send."""

import os
import time
from pathlib import Path

from agent_comms.child_process import Platform
from agent_comms.field_codec import FieldCodec
from agent_comms.installer.interpreter_operation import (
    GitPhase,
    InstallClock,
    LaunchedPhaseTiming,
    LauncherPhase,
    PhaseTiming,
    ProcessLauncherPhase,
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


def test_a_launch_inside_a_long_lived_process_does_not_count_the_process_age():
    platform = Platform.current()
    age = time.monotonic() - platform.started_at(platform.identity(os.getpid()))
    clock = InstallClock()

    with LauncherPhase().running(clock) as hosted:
        pass
    with ProcessLauncherPhase().running(clock) as launched:
        pass

    assert age > 0.1
    assert hosted.seconds < 0.05
    assert launched.seconds >= age


def test_a_process_launcher_timing_keeps_the_deployed_launcher_wire():
    timing = PhaseTiming(ProcessLauncherPhase(), 1.0, 1.0)

    assert FieldCodec.encode(timing.launched()) == LAUNCHER_PHASES[0]
