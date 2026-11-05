"""LaunchRoot reads this process's own arguments, never a command it will run."""

from agent_comms.launch_root import LaunchRoot


def test_a_child_command_flag_does_not_select_the_parents_root(tmp_path):
    """A `--` ends this process's options; a child's --comms-root belongs to the child.

    Reading it here selected the child's root for this process, which then failed
    with an unrelated thread-registration error instead of naming the real mistake.
    """
    mine, child = tmp_path / "mine", tmp_path / "child"
    mine.mkdir()
    child.mkdir()
    published = LaunchRoot.from_path(mine)
    arguments = ["--", "prog", "--comms-root", str(child)]

    root = LaunchRoot.startup(arguments, lambda: published)

    assert root.path == mine.absolute()
    assert arguments == ["--", "prog", "--comms-root", str(child)], (
        "the child's own arguments are left exactly as written"
    )


def test_this_processes_own_root_flag_is_still_read_and_removed(tmp_path):
    mine = tmp_path / "mine"
    mine.mkdir()
    arguments = ["--comms-root", str(mine), "--thread", "reader"]

    root = LaunchRoot.startup(arguments, lambda: LaunchRoot.from_path(tmp_path / "wrong"))

    assert root.path == mine.absolute()
    assert arguments == ["--thread", "reader"]
