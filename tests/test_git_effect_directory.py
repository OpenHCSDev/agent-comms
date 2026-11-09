"""A Git directory is a storage destination only when its effect writes there."""

from pathlib import Path

import pytest

from agent_comms.projects.shell_effects import (
    ForbiddenStorageEffect,
    WriteStorageEffect,
    shell_storage_effects,
)
from agent_comms.transport.tool_denial import StorageDenied


@pytest.mark.parametrize("command", [
    'for p in /home/ts/code/projects/*/; do git -C "$p" remote -v | grep -qi toad && echo "$p"; done',
    'git -C "$repo" status --short',
    'git --git-dir="$repo" log -1',
    'git -C "$repo" --work-tree="$tree" diff --stat',
])
def test_read_only_git_does_not_resolve_a_storage_destination(command: str, tmp_path: Path):
    assert shell_storage_effects(command, tmp_path) == ()


@pytest.mark.parametrize("command", [
    'git -C "$repo" reset --hard',
    'for p in /home/ts/code/projects/*/; do git -C "$p" reset --hard; done',
    'for p in /home/ts/code/projects/*/; do touch "$p/file"; done',
    'git --git-dir="$repo" commit -am change',
    'git --work-tree="$tree" checkout main',
    'for p in /home/ts/code/projects/*/; do git -C "$p" remote -v; touch "$p/file"; done',
    'git -C "$repo" remote -v > "$output"',
    'cd "$repo"; touch file',
])
def test_unresolved_write_destinations_still_fail(command: str, tmp_path: Path):
    with pytest.raises(StorageDenied, match="literal storage destination"):
        shell_storage_effects(command, tmp_path)


@pytest.mark.parametrize("command", [
    'git -C "$repo" clone source destination',
    'git -C "$repo" worktree add destination',
    'for p in /home/ts/code/projects/*/; do git -C "$p" worktree add destination; done',
    'git --git-dir="$repo" worktree remove destination',
])
def test_forbidden_git_operations_do_not_become_reads(command: str, tmp_path: Path):
    effects = shell_storage_effects(command, tmp_path)
    assert len(effects) == 1
    assert isinstance(effects[0], ForbiddenStorageEffect)


def test_loop_body_changes_directory_before_the_next_write(tmp_path: Path):
    effects = shell_storage_effects("for p in literal; do cd target; touch file; done", tmp_path)
    assert effects == (WriteStorageEffect((tmp_path / "target" / "file").resolve()),)


def test_loop_keyword_in_an_argument_does_not_become_a_command(tmp_path: Path):
    assert shell_storage_effects("echo do git -C '$repo' reset --hard", tmp_path) == ()


def test_git_write_uses_its_final_literal_directory(tmp_path: Path):
    effects = shell_storage_effects("git -C outer -C ../target reset --hard", tmp_path)
    assert effects == (WriteStorageEffect((tmp_path / "target").resolve()),)
