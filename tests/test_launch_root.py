"""The published route is the default; explicit roots win and environments do not."""
import json
import os
import subprocess
import sys
from dataclasses import fields
from pathlib import Path

import pytest

from agent_comms.active_route import ActiveRoute, resolve_comms_route
from agent_comms.cli import main
from agent_comms.comms import Comms
from agent_comms.errors import RelationViolationError
from agent_comms.launch_root import LaunchRoot
from agent_comms.launch_runtime import LaunchRuntime, RestartEnvironment, RestoredRuntime
from agent_comms.route_selection import RouteSelection, child_root


@pytest.fixture(autouse=True)
def no_process_selection(monkeypatch):
    monkeypatch.setattr(LaunchRoot, '_current', LaunchRoot.unstarted())


def publish_route(tmp_path):
    root = tmp_path / 'published'
    comms = Comms(root)
    root_id = comms.messaging.initialize_private_initial_protocol()
    comms.registry.declare(comms.registry.new_thread('worker', frozenset(), str(tmp_path)))
    route = tmp_path / '.local/state/agent-comms/active-route.json'
    route.parent.mkdir(mode=0o700, parents=True)
    route.write_text(json.dumps(ActiveRoute(root, root_id).to_record()))
    route.chmod(0o600)
    return root


def test_rootless_client_uses_published_route_not_environment(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    root = publish_route(tmp_path)
    unrelated = tmp_path / 'wrong'
    monkeypatch.setenv('AGENT_COMMS_ROOT', str(unrelated))
    assert resolve_comms_route().root == root
    assert child_root(dict(os.environ), tmp_path) == root
    assert main(['threads']) == 0
    assert not unrelated.exists()


def test_rootless_process_pins_its_route_before_launching_children(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    first = publish_route(tmp_path)
    selected = LaunchRoot.current()
    second = tmp_path / 'second'
    second_id = Comms(second).messaging.initialize_private_initial_protocol()
    route = tmp_path / '.local/state/agent-comms/active-route.json'
    route.write_text(json.dumps(ActiveRoute(second, second_id).to_record()))
    assert LaunchRoot.current() is selected
    assert child_root({'AGENT_COMMS_ROOT': str(second)}, tmp_path) == first
    assert selected.arguments() == ('--comms-root', str(first))


def test_selection_is_scoped_and_child_ignores_home_and_environment(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    selected = tmp_path / 'copy'
    with LaunchRoot.from_path(selected).selected():
        assert resolve_comms_route().observe_root() == selected
        assert child_root({'HOME': '/different', 'AGENT_COMMS_ROOT': '/live'}, '/') == selected
        assert RouteSelection.for_child({}, '/').root == selected
        with LaunchRoot.from_path(tmp_path / 'second').selected():
            assert LaunchRoot.current().path == tmp_path / 'second'
        assert LaunchRoot.current().path == selected
    with pytest.raises(RelationViolationError, match='No active Comms route is published'):
        LaunchRoot.current()


@pytest.mark.parametrize('runtime', [LaunchRuntime, RestoredRuntime])
def test_every_launch_policy_removes_root_but_keeps_other_inheritance(runtime, tmp_path):
    source = {'AGENT_COMMS_ROOT': '/live', 'HOME': str(tmp_path), 'CREDENTIAL': 'kept'}
    result = runtime(Path(sys.executable)).environment(source)
    assert 'AGENT_COMMS_ROOT' not in result
    assert result['CREDENTIAL'] == 'kept'
    assert source['AGENT_COMMS_ROOT'] == '/live'
    assert 'root' not in {field.name for field in fields(RestartEnvironment)}
    assert 'AGENT_COMMS_ROOT' not in RestartEnvironment.inherit(source).encode()


def test_root_fixes_relative_meaning_without_hiding_symlinks(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    root = LaunchRoot.from_path(Path('copy'))
    later = tmp_path / 'later'
    later.mkdir()
    monkeypatch.chdir(later)
    assert root.path == tmp_path / 'copy'
    link = tmp_path / 'alias'
    link.symlink_to(later)
    assert LaunchRoot.from_path(link).path == link


def test_launch_argument_is_consumed_by_its_owner(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    published = publish_route(tmp_path)
    explicit = tmp_path / 'explicit'
    arguments = ['program', *LaunchRoot.from_path(explicit).arguments(), 'threads']
    LaunchRoot.startup(arguments)
    assert arguments == ['program', 'threads']
    assert LaunchRoot.current().path == explicit
    assert LaunchRoot.current().path != published
    assert LaunchRoot.current().arguments() == ('--comms-root', str(explicit))


def test_rootless_real_cli_child_fails_without_published_route(tmp_path):
    environment = {**os.environ, 'HOME': str(tmp_path), 'AGENT_COMMS_ROOT': '/live'}
    environment = LaunchRuntime(Path(sys.executable)).environment(environment)
    # Explicitly select the source under test, not an older installed module.
    script = ('import sys; sys.path.insert(0, ' + repr(str(Path(__file__).parents[1] / 'src')) + '); '
              'from agent_comms.cli import main; raise SystemExit(main(["threads"]))')
    result = subprocess.run([sys.executable, '-c', script], env=environment,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 1
    assert 'No active Comms route is published' in result.stdout
    assert not (tmp_path / '.agent-comms').exists()


def test_rootless_real_cli_child_opens_published_route(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    publish_route(tmp_path)
    environment = {**os.environ, 'HOME': str(tmp_path), 'AGENT_COMMS_ROOT': '/wrong'}
    environment = LaunchRuntime(Path(sys.executable)).environment(environment)
    script = ('import sys; sys.path.insert(0, ' + repr(str(Path(__file__).parents[1] / 'src')) + '); '
              'from agent_comms.cli import main; raise SystemExit(main(["threads"]))')
    result = subprocess.run([sys.executable, '-c', script], env=environment,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'worker' in result.stdout
