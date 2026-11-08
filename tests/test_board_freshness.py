"""The board follows its stores, not the heartbeat's clock.

A frame can equal the last completed pass and still describe a team that moved after it. The
question a reader can answer without a heartbeat is whether a store the board derives from is
newer than the frames it published; that is what decides a rebuild.
"""

import os
from pathlib import Path

from test_holder_standing_rules import held_bus  # noqa: F401 - the wired bus fixture

from agent_comms.team_frame_file import TeamFrameFile
from agent_comms.team_hud import TeamFrame
from agent_comms.team_hud import TeamPlanFile
from agent_comms.service_passes import ServicePassStore
from agent_comms.todos import TodoStore


def board_roots(tmp_path: Path) -> tuple[Path, ...]:
    return (TeamPlanFile.path_in(tmp_path), ServicePassStore.path_in(tmp_path),
            TodoStore.path_in(tmp_path))


def published_frame(tmp_path: Path, *, newest: bool) -> Path:
    frame = tmp_path / "team_frames" / "worker.json"
    frame.parent.mkdir(parents=True, exist_ok=True)
    frame.write_text("{}")
    for index, path in enumerate(board_roots(tmp_path)):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}")
        stamp = 2_000.0 if newest else 1_000.0 + index
        os.utime(path, (stamp, stamp))
    os.utime(frame, (1_500.0, 1_500.0))
    return frame


def test_a_board_with_no_frame_is_owed_one(tmp_path):
    assert TeamFrame.owed(tmp_path) is True


def test_a_frame_newer_than_its_stores_is_current(tmp_path):
    published_frame(tmp_path, newest=False)
    assert TeamFrame.owed(tmp_path) is False


def test_a_store_written_after_the_frame_makes_the_board_stale(tmp_path):
    frame = published_frame(tmp_path, newest=False)
    os.utime(TodoStore.path_in(tmp_path), (2_000.0, 2_000.0))
    os.utime(frame, (1_500.0, 1_500.0))
    assert TeamFrame.owed(tmp_path) is True


def test_the_watched_paths_include_the_todo_store(tmp_path):
    assert TodoStore.path_in(tmp_path) in TeamFrameFile.source_paths(tmp_path)


def test_a_board_read_rebuilds_without_any_heartbeat_pass(held_bus):
    """The proof this order owes: no frame on the bus, no pass run, a board nonetheless.

    `TeamHudReading.refreshed` is what a view's read now calls; before it, the only path to a
    board was `TeamFrame.read_all`, which returns nothing until a heartbeat writes a frame.
    """
    from agent_comms.team_hud import TeamFrame
    from agent_comms.ui_model.observation import TeamHudReading

    assert TeamFrame.read_all(held_bus.root) == (), "the bus starts with no published board"
    board = TeamHudReading.refreshed(held_bus)
    assert board, "a read rebuilds the board it displays without waiting for a heartbeat"
    assert TeamFrame.read_all(held_bus.root) == board, "the rebuilt board is the retained one"
