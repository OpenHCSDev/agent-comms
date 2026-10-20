"""A launched root is explicit; child environment and default files grant none."""
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


def test_rootless_client_cannot_borrow_published_route_or_environment(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    root = tmp_path / 'published'
    root_id = Comms(root).messaging.initialize_private_initial_protocol()
    route = tmp_path / '.local/state/agent-comms/active-route.json'
    route.parent.mkdir(mode=0o700, parents=True)
    route.write_text(json.dumps(ActiveRoute(root, root_id).to_record()))
    route.chmod(0o600)
    monkeypatch.setenv('AGENT_COMMS_ROOT', str(root))
    with pytest.raises(RelationViolationError, match='explicit launch root'):
        resolve_comms_route()
    with pytest.raises(RelationViolationError, match='explicit launch root'):
        child_root(dict(os.environ), tmp_path)
    assert main(['threads']) == 1
    assert not (root / 'registry.json').exists()


def test_selection_is_scoped_and_child_ignores_home_and_environment(tmp_path):
    selected = tmp_path / 'copy'
    with LaunchRoot.from_path(selected).selected():
        assert resolve_comms_route().observe_root() == selected
        assert child_root({'HOME': '/different', 'AGENT_COMMS_ROOT': '/live'}, '/') == selected
        assert RouteSelection.for_child({}, '/').root == selected
        with LaunchRoot.from_path(tmp_path / 'second').selected():
            assert LaunchRoot.current().path == tmp_path / 'second'
        assert LaunchRoot.current().path == selected
    with pytest.raises(RelationViolationError, match='explicit launch root'):
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


def test_launch_argument_is_consumed_by_its_owner(tmp_path):
    arguments = ['program', *LaunchRoot.from_path(tmp_path).arguments(), 'threads']
    LaunchRoot.startup(arguments)
    assert arguments == ['program', 'threads']
    assert LaunchRoot.current().path == tmp_path


def test_rootless_real_cli_child_fails_before_using_default(tmp_path):
    environment = {**os.environ, 'HOME': str(tmp_path), 'AGENT_COMMS_ROOT': '/live'}
    environment = LaunchRuntime(Path(sys.executable)).environment(environment)
    # Explicitly select the source under test, not an older installed module.
    script = ('import sys; sys.path.insert(0, ' + repr(str(Path(__file__).parents[1] / 'src')) + '); '
              'from agent_comms.cli import main; raise SystemExit(main(["threads"]))')
    result = subprocess.run([sys.executable, '-c', script], env=environment,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 1
    assert 'explicit launch root' in result.stdout
    assert not (tmp_path / '.agent-comms').exists()
