"""Route observation shares private marker validation without service/store reads."""

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from agent_comms.active_route import ActiveRoute, CommsRoute, LocalRoute, read_active_route, resolve_comms_route
from agent_comms.launch_root import LaunchRoot
from agent_comms.comms import Comms, wire
from agent_comms.errors import RelationViolationError
import agent_comms.private_nk_entrypoint as private_nk_entrypoint
from agent_comms.private_nk_entrypoint import PrivateNkLaunch
from agent_comms.registration import Registration
from agent_comms.registry_document import RegistryDocument
from agent_comms.wire_log import WireLog


@pytest.fixture(autouse=True)
def installed_package(verified_native_package, monkeypatch):
    """This installation's pinned native package; a route no longer names one."""
    monkeypatch.setattr(private_nk_entrypoint, "pinned_native_package", lambda: verified_native_package.path)


def route_fixture(tmp_path, monkeypatch):
    tmp_path.chmod(0o700)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(LaunchRoot, "_current", LaunchRoot.unstarted())
    monkeypatch.delenv("AGENT_COMMS_ROOT", raising=False)
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    comms = Comms(root)
    root_id = comms.messaging.initialize_private_initial_protocol()
    route = tmp_path / ".local/state/agent-comms/active-route.json"
    route.parent.mkdir(parents=True, mode=0o700)
    install_route(route, root, root_id)
    return route, root, root_id


def install_route(route, root, root_id):
    stage = route.with_suffix(".new")
    stage.write_text(json.dumps(dict(version=2, root=str(root), wire_root_id=root_id)))
    stage.chmod(0o600)
    os.replace(stage, route)


def test_observation_never_composes_decodes_or_waits_for_bus_lock(tmp_path, monkeypatch):
    route, root, root_id = route_fixture(tmp_path, monkeypatch)
    # Hold the actual exclusive mutation lock: observing route identity must
    # still finish. Public service binding intentionally retains this barrier.
    with (WireLog(root / "bus.jsonl").locked(),
          patch.object(Comms, "__init__", side_effect=AssertionError("composed service")),
          patch.object(Registration, "__init__", side_effect=AssertionError("read registry")),
          patch.object(RegistryDocument, "from_wire", side_effect=AssertionError("decoded registry")),
          patch.object(WireLog, "locked", side_effect=AssertionError("acquired mutation lock"))):
        selected = CommsRoute.current()
        assert selected == ActiveRoute(root, root_id)
        assert selected.observe_root() == root
        assert list(tmp_path.glob("private*/registry.json")) == []


def test_observation_revalidates_rotation_id_permissions_and_shape(tmp_path, monkeypatch):
    route, root, root_id = route_fixture(tmp_path, monkeypatch)
    second = tmp_path / "second"
    second_id = Comms(second).messaging.initialize_private_initial_protocol()
    assert CommsRoute.current().observe_root() == root
    install_route(route, second, second_id)
    assert CommsRoute.current().observe_root() == second
    install_route(route, second, root_id)
    with pytest.raises(RelationViolationError, match="root ID"):
        CommsRoute.current().observe_root()
    install_route(route, second, second_id)
    marker = second / "bus_meta.json"
    marker.chmod(0o644)
    with pytest.raises(RelationViolationError, match="owner-only"):
        CommsRoute.current().observe_root()
    marker.chmod(0o600)
    marker.write_text('{"writer_protocol_version":999}')
    with pytest.raises(RelationViolationError):
        CommsRoute.current().observe_root()


def test_explicit_environment_absent_default_and_invalid_route(tmp_path, monkeypatch):
    route, root, root_id = route_fixture(tmp_path, monkeypatch)
    route.write_text("invalid")
    with pytest.raises(ValueError):
        CommsRoute.current().observe_root()
    explicit = tmp_path / "uncreated"
    assert resolve_comms_route(explicit) == LocalRoute(explicit)
    assert resolve_comms_route(explicit).observe_root() == explicit
    monkeypatch.setenv("AGENT_COMMS_ROOT", str(explicit))
    with pytest.raises(ValueError, match="active comms route is invalid JSON"):
        LaunchRoot.startup(['agent-comms'], ActiveRoute.published_launch)
    with LaunchRoot.from_path(explicit).selected():
        assert resolve_comms_route().observe_root() == explicit
    assert not explicit.exists(), "Observation must not create a wire"
    monkeypatch.delenv("AGENT_COMMS_ROOT")
    route.unlink()
    assert CommsRoute.current().observe_root() == tmp_path / ".agent-comms"
    with pytest.raises(RelationViolationError, match="No active Comms route is published"):
        LaunchRoot.startup(['agent-comms'], ActiveRoute.published_launch)
    assert not (tmp_path / ".agent-comms").exists()


def test_rootless_service_rejects_published_root_with_wrong_marker_id(tmp_path, monkeypatch):
    route, root, _ = route_fixture(tmp_path, monkeypatch)
    route.write_text(json.dumps(dict(version=2, root=str(root), wire_root_id='0' * 32)))
    with pytest.raises(RelationViolationError, match='root ID'):
        LaunchRoot.startup(['agent-comms'], ActiveRoute.published_launch)


def test_service_factory_uses_selection_and_retains_private_pin(tmp_path, monkeypatch, verified_native_package):
    _, root, root_id = route_fixture(tmp_path, monkeypatch)
    LaunchRoot.from_path(root).install()
    service = wire()
    assert service.root == root
    # The default and an explicit selection of the same root derive one launch.
    assert service.owners.launch_authority == PrivateNkLaunch(root, root_id, verified_native_package)
    assert wire(root).owners.launch_authority == service.owners.launch_authority


def test_route_record_retains_the_external_format_and_private_marker(tmp_path):
    tmp_path.chmod(0o700)
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    root_id = Comms(root).messaging.initialize_private_initial_protocol()
    selected = ActiveRoute(root, root_id)
    encoded = selected.to_record()
    assert encoded == {"version": 2, "root": str(root), "wire_root_id": root_id}
    route_file = tmp_path / "active-route.json"
    route_file.write_text(json.dumps(encoded))
    route_file.chmod(0o600)
    assert read_active_route(route_file) == selected
    assert read_active_route(route_file).observe_root() == root
    assert not (root / "registry.json").exists()


@pytest.mark.parametrize("change", [
    {"version": True}, {"version": "2"}, {"version": 1}, {"version": None},
    {"root": ""}, {"root": "relative"}, {"root": "/tmp/../private"}, {"root": None},
    {"native_package": str(Path("/package"))}, {"wire_root_id": 9},
    {"wire_root_id": "A" * 32}, {"wire_root_id": "a" * 31}, {"unexpected": True},
])
def test_malformed_route_identity_is_denied_at_the_real_file_boundary(tmp_path, change):
    tmp_path.chmod(0o700)
    route_file = tmp_path / "active-route.json"
    value = ActiveRoute(tmp_path, "a" * 32).to_record()
    value.update(change)
    route_file.write_text(json.dumps(value))
    route_file.chmod(0o600)
    with pytest.raises(ValueError):
        read_active_route(route_file)
    assert not (tmp_path / "registry.json").exists()


@pytest.mark.parametrize("missing", ["version", "root", "wire_root_id"])
def test_route_fields_remain_required_even_with_constructor_defaults(tmp_path, missing):
    value = ActiveRoute(tmp_path, "a" * 32).to_record()
    value.pop(missing)
    with pytest.raises(ValueError):
        ActiveRoute.from_record(value, tmp_path / "active-route.json")
