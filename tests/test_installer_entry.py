"""The interpreter entry preserves caller policy and isolates old target-source launches."""

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Never

import pytest

import agent_comms.installer
from agent_comms.installer import __main__ as entry
from agent_comms.installer import bootstrap


@dataclass
class ExecSelection(Exception):
    executable: str
    arguments: tuple[str, ...]


def selected_exec(executable: str, arguments: tuple[str, ...]) -> Never:
    raise ExecSelection(executable, arguments)


@pytest.mark.parametrize("target_source", (False, True))
def test_entry_keeps_caller_policy_and_isolates_target_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, target_source: bool,
) -> None:
    root = Path(entry.__file__).resolve().parents[3] if target_source else tmp_path
    monkeypatch.chdir(root)
    monkeypatch.setattr(os, "execv", selected_exec)
    with pytest.raises(ExecSelection) as selected:
        entry.main()
    request = selected.value
    assert request.executable == sys.executable
    assert request.arguments[0] == sys.executable
    assert request.arguments[-2:] == (
        "-m", agent_comms.installer.__name__ if target_source else bootstrap.__name__,
    )
    assert request.arguments[1:-2] == (
        ("-I",) if target_source or sys.flags.isolated else ()
    )
