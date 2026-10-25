"""Every command failure crosses a worker's pipe whole, whatever its constructor takes."""

import importlib
import pickle
import pkgutil
import subprocess

import pytest

import agent_comms
from agent_comms.coordination_errors import CommandFailure
from agent_comms.repo import GitCommandError


def members(family: type) -> set[type]:
    return {member for child in family.__subclasses__() for member in {child, *members(child)}}


def declared() -> list[type[CommandFailure]]:
    """The family once every module declaring a member is imported."""
    for module in pkgutil.walk_packages(agent_comms.__path__, f"{agent_comms.__name__}."):
        if ".skills." not in module.name:
            importlib.import_module(module.name)
    return sorted(members(CommandFailure), key=lambda member: member.__qualname__)


@pytest.mark.parametrize("member", declared(), ids=lambda member: member.__qualname__)
def test_each_member_unpickles_with_its_message_state_and_cause(member) -> None:
    failure = member.__new__(member, "git exited 128")
    failure.detail = "kept"
    failure.__cause__ = ValueError("root")
    copy = pickle.loads(pickle.dumps(failure))
    assert type(copy) is member
    assert copy.args == ("git exited 128",)
    assert copy.detail == "kept"
    assert copy.__cause__.args == ("root",)


def test_a_raised_git_failure_keeps_its_git_message() -> None:
    run = subprocess.CompletedProcess(("git",), 128, b"", b"fatal: bad object\n")
    copy = pickle.loads(pickle.dumps(GitCommandError("/repo", run)))
    assert str(copy) == "Git in /repo: fatal: bad object"
    assert (copy.returncode, copy.stderr) == (128, "fatal: bad object\n")
