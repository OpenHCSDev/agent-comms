"""A repository shares one prefix but retains the exact chosen declaration."""
import json
import os
from dataclasses import replace
from pathlib import Path
import subprocess
import sys

import pytest

from agent_comms.cli_verbs.sync_test_environment import SyncTestEnvironmentCliCommand
from agent_comms.installer.refusals import UpgradeRefused
from agent_comms.registry.registry_document import RegisteredTeamProject
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
    Repo.git(path, 'remote', 'add', 'origin', f'https://github.com/example/{path.name}.git')
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
        adopted = selected.sync(commands, test_uv, Path(sys.executable), start=lent())
        assert adopted['completed']
        retained = json.loads(Path(adopted['previous']).read_text())
        assert retained['receipt'] == ''
        assert retained['packages'] == []
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
    comms.registry.set_team_project(RegisteredTeamProject(repos=(Repo.from_origin(unrelated.path),)))
    with pytest.raises(FileNotFoundError, match='missing.txt'):
        replace(command, requirements=Path('missing.txt')).apply(comms)
    assert not unrelated.test_environment.exists()


def test_shared_prefix_cannot_follow_an_unrelated_environment(tmp_path: Path):
    checkout = committed_checkout(tmp_path / 'repository')
    elsewhere = tmp_path / 'retained-environment'
    elsewhere.mkdir()
    marker = elsewhere / 'retained-source.py'
    marker.write_text('preserve me\n')
    checkout.test_environment.symlink_to(elsewhere, target_is_directory=True)
    with pytest.raises(ValueError, match='physical prefix'):
        DeclaredTestEnvironment(checkout, Path('requirements-dev.txt')).require_unused()
    assert marker.read_text() == 'preserve me\n'


def test_locked_project_rebuilds_editable_modules_and_retains_old_sources(
        tmp_path: Path, test_uv: Path, integration_lease: LeaseStart):
    checkout = committed_checkout(tmp_path / 'project')
    package = checkout.path / 'fixture_package'
    package.mkdir()
    (package / '__init__.py').write_text('VALUE = 17\n')
    declaration = checkout.path / 'pyproject.toml'
    declaration.write_text('''[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"
[project]
name = "fixture-project"
version = "0.1.0"
requires-python = ">=3.12"
[project.optional-dependencies]
dev = ["packaging"]
[tool.setuptools]
packages = ["fixture_package"]
py-modules = []
''')
    def commit():
        Repo.git(checkout.path, 'add', 'pyproject.toml', 'uv.lock', 'fixture_package')
        Repo.git(checkout.path, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.org',
                 'commit', '-qm', 'project declaration')

    def lent() -> LeaseStart:
        descriptors = integration_lease.descriptors()
        return InheritedTestRun(os.dup(descriptors[0])) if descriptors else integration_lease

    selected = DeclaredTestEnvironment.selected(checkout, Path('pyproject.toml'))
    python = selected.environment.prefix / 'bin/python'
    with InstallCommands() as commands:
        commands.run(test_uv, 'lock', '--project', checkout.path, '--python', sys.executable)
        commit()
        before = selected.sync(commands, test_uv, Path(sys.executable), start=lent())
        assert before['completed']
        # Isolated mode and a foreign cwd prevent source-root imports hiding a stale finder.
        probe = ('import fixture_package, packaging, importlib.util; '
                 'assert fixture_package.VALUE == 17; '
                 'print(importlib.util.find_spec("fixture_trace") is not None)')
        assert commands.run(python, '-I', '-c', probe, cwd=tmp_path).strip() == 'False'
        selected.environment.declaration_record.unlink()
        declaration.write_text(declaration.read_text().replace('py-modules = []',
                                                              'py-modules = ["fixture_trace"]'))
        (checkout.path / 'fixture_trace.py').write_text('VALUE = 29\n')
        Repo.git(checkout.path, 'add', 'fixture_trace.py')
        commit()
        record = selected.sync(commands, test_uv, Path(sys.executable), start=lent())
        assert record['completed']
        retained = json.loads(Path(record['previous']).read_text())
        assert any(package['name'] == 'fixture-project' for package in retained['packages'])
        assert str(checkout.path) in retained['sources']
        assert retained['receipt'] == ''
        assert commands.run(python, '-I', '-c', probe, cwd=tmp_path).strip() == 'True'
        assert commands.run(python, '-I', '-c',
                            'import fixture_trace; print(fixture_trace.VALUE)',
                            cwd=tmp_path).strip() == '29'
        saved = selected.environment.declaration_record.read_bytes()
        lock = checkout.path / 'uv.lock'
        old_lock = lock.read_bytes()
        lock.write_bytes(old_lock + b'\n# not committed\n')
        with pytest.raises(ValueError, match='Commit uv.lock'):
            selected.sync(commands, test_uv, Path(sys.executable), start=lent())
        lock.write_bytes(old_lock)
        (package / '__init__.py').write_text('VALUE = 999\n')
        with pytest.raises(ValueError, match='Commit the project source'):
            selected.sync(commands, test_uv, Path(sys.executable), start=lent())
        assert selected.environment.declaration_record.read_bytes() == saved
        (package / '__init__.py').write_text('VALUE = 17\n')
        declaration.write_text(declaration.read_text().replace('version = "0.1.0"', 'version = "0.2.0"'))
        commit()
        with pytest.raises(UpgradeRefused, match='lockfile.*updated'):
            selected.sync(commands, test_uv, Path(sys.executable), start=lent())
        assert selected.environment.declaration_record.read_bytes() == saved
