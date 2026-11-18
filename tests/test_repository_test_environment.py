"""A repository shares one prefix but retains the exact chosen declaration."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from agent_comms.cli_verbs.sync_test_environment import SyncTestEnvironmentCliCommand
from agent_comms.registry.threads import NativeThread
from agent_comms.transport.tool_caller import PeerCaller
from agent_comms.execution.launch_runtime import LaunchRuntime
from agent_comms.execution.test_lease import InheritedTestRun, LeaseStart
from agent_comms.installer.commands import InstallCommands
from agent_comms.projects.checkouts import Checkout
from agent_comms.projects.repo import Repo
from agent_comms.projects.repository_environment import RepositoryEnvironment
from agent_comms.projects.test_environment import DeclaredTestEnvironment


def committed_checkout(path: Path) -> Checkout:
    path.mkdir()
    Repo.git(path, 'init', '-q')
    (path / 'requirements-dev.txt').write_text('# No dependencies in this control.\n')
    Repo.git(path, 'add', 'requirements-dev.txt')
    Repo.git(path, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.org',
             'commit', '-qm', 'declaration')
    return Checkout(path)


@pytest.mark.parametrize('separate_git_dir', [False, True])
def test_worktrees_share_main_environment(tmp_path: Path, separate_git_dir: bool):
    main = committed_checkout(tmp_path / 'main')
    if separate_git_dir:
        Repo.git(main.path, 'init', '--separate-git-dir', str(tmp_path / 'git-store'))
        Repo.git(main.path, 'config', 'core.worktree', str(main.path))
    linked = tmp_path / 'linked'
    Repo.git(main.path, 'worktree', 'add', '--detach', str(linked))
    other = Checkout(linked)
    assert main.test_environment == other.test_environment == main.path / '.venv'
    assert RepositoryEnvironment.of(main.path) == RepositoryEnvironment.of(linked)
    (linked / 'requirements-dev.txt').write_text('# Changed but not committed.\n')
    selected = DeclaredTestEnvironment(other, Path('requirements-dev.txt'))
    with pytest.raises(ValueError, match='Commit requirements-dev.txt'):
        selected.source()
    assert not main.test_environment.exists()
    (linked / 'requirements-dev.txt').write_text('# A different committed declaration.\n')
    Repo.git(linked, 'add', 'requirements-dev.txt')
    Repo.git(linked, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.org',
             'commit', '-qm', 'different input')
    declaration, revision, _ = selected.source()
    assert declaration == linked / 'requirements-dev.txt'
    assert revision == Repo.git(linked, 'rev-parse', 'HEAD').strip()
    assert revision != Repo.git(main.path, 'rev-parse', 'HEAD').strip()
    with pytest.raises(ValueError, match='escape'):
        DeclaredTestEnvironment(other, main.path / 'requirements-dev.txt').source()


def test_bare_repository_does_not_borrow_sibling_prefix(tmp_path: Path):
    bare = tmp_path / 'bare.git'
    Repo.git(tmp_path, 'init', '--bare', '-q', str(bare))
    assert RepositoryEnvironment.of(bare).prefix == bare / 'test-environment'


def test_real_sync_and_live_user_protection(tmp_path: Path, test_uv: Path,
                                           integration_lease: LeaseStart):
    checkout = committed_checkout(tmp_path / 'repository')
    selected = DeclaredTestEnvironment(checkout, Path('requirements-dev.txt'))
    # The outer pytest already holds the host test lease. Adopt it through the same
    # owner as installer tests, rather than recursively asking for a new lease.
    def lent() -> LeaseStart:
        descriptors = integration_lease.descriptors()
        return InheritedTestRun(os.dup(descriptors[0])) if descriptors else integration_lease

    with InstallCommands() as commands:
        declaration, revision, digest = selected.source()
        prefix = selected.environment.prefix
        record = selected.sync(commands, test_uv, Path(sys.executable), start=lent())
        assert record['completed'] is True
        assert json.loads(selected.environment.declaration_record.read_text()) == record
        assert selected.sync(commands, test_uv, Path(sys.executable), start=lent())['completed']
        selected.environment.declaration_record.unlink()
        with pytest.raises(ValueError, match='no requirements sync record'):
            selected.sync(commands, test_uv, Path(sys.executable), start=lent())
        # This empty environment is a real user of the same prefix, including when
        # it is a child of this test process (installer retirement excludes children).
        python = LaunchRuntime.at_prefix(prefix).interpreter
        with subprocess.Popen([str(python), '-c', 'import sys; print("ready", flush=True); sys.stdin.read()'],
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True) as child:
            assert child.stdout.readline().strip() == 'ready'
            with pytest.raises(ValueError, match='in use'):
                selected.require_unused()
            child.communicate('', timeout=10)
            assert child.returncode == 0
        selected.require_unused()
        # Source selection has no package-name copies in Core.
        assert declaration == checkout.path / 'requirements-dev.txt'
        assert revision and digest


def test_sync_cannot_select_another_registered_projects_repository(tmp_path: Path, comms):
    project = committed_checkout(tmp_path / 'project')
    unrelated = committed_checkout(tmp_path / 'unrelated')
    comms.registry.register(NativeThread(name='project-owner', tags=frozenset(), worktree=project))
    command = SyncTestEnvironmentCliCommand(
        caller=PeerCaller('project-owner'), project_thread='project-owner',
        checkout=unrelated.path, requirements=Path('requirements-dev.txt'))
    with pytest.raises(ValueError, match="not a worktree of project-owner's repository"):
        command.apply(comms)
    assert not project.test_environment.exists()
    assert not unrelated.test_environment.exists()
