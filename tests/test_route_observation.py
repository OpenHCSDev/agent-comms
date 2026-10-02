"""Route observation shares private marker validation without service/store reads."""

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from agent_comms.active_route import ActiveRoute, LocalRoute, read_active_route, resolve_comms_route
from agent_comms.comms import Comms, wire
from agent_comms.errors import RelationViolationError
from agent_comms.field_codec import FieldCodec
from agent_comms.private_nk_entrypoint import PrivateNkLaunch
from agent_comms.registration import Registration
from agent_comms.registry_document import RegistryDocument
from agent_comms.wire_log import WireLog


def route_fixture(tmp_path, monkeypatch):
    tmp_path.chmod(0o700)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("AGENT_COMMS_ROOT", raising=False)
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    comms = Comms(root)
    root_id = comms.messaging.initialize_private_initial_protocol()
    route = tmp_path / ".local/state/agent-comms/active-route.json"
    route.parent.mkdir(parents=True, mode=0o700)
    install_route(route, root, root_id, tmp_path)
    return route, root, root_id


def install_route(route, root, root_id, package):
    stage = route.with_suffix(".new")
    stage.write_text(json.dumps(dict(version=1, root=str(root), wire_root_id=root_id,
                                    native_package=str(package))))
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
        selected = resolve_comms_route()
        assert selected == ActiveRoute(root, root_id, tmp_path)
        assert selected.observe_root() == root
        assert list(tmp_path.glob("private*/registry.json")) == []


def test_observation_revalidates_rotation_id_permissions_and_shape(tmp_path, monkeypatch):
    route, root, root_id = route_fixture(tmp_path, monkeypatch)
    second = tmp_path / "second"
    second_id = Comms(second).messaging.initialize_private_initial_protocol()
    assert resolve_comms_route().observe_root() == root
    install_route(route, second, second_id, tmp_path)
    assert resolve_comms_route().observe_root() == second
    install_route(route, second, root_id, tmp_path)
    with pytest.raises(RelationViolationError, match="root ID"):
        resolve_comms_route().observe_root()
    install_route(route, second, second_id, tmp_path)
    marker = second / "bus_meta.json"
    marker.chmod(0o644)
    with pytest.raises(RelationViolationError, match="owner-only"):
        resolve_comms_route().observe_root()
    marker.chmod(0o600)
    marker.write_text('{"writer_protocol_version":999}')
    with pytest.raises(RelationViolationError):
        resolve_comms_route().observe_root()


def test_explicit_environment_absent_default_and_invalid_route(tmp_path, monkeypatch):
    route, root, root_id = route_fixture(tmp_path, monkeypatch)
    route.write_text("invalid")
    with pytest.raises(ValueError):
        resolve_comms_route().observe_root()
    explicit = tmp_path / "uncreated"
    assert resolve_comms_route(explicit) == LocalRoute(explicit)
    assert resolve_comms_route(explicit).observe_root() == explicit
    monkeypatch.setenv("AGENT_COMMS_ROOT", str(explicit))
    assert resolve_comms_route().observe_root() == explicit
    assert not explicit.exists(), "Observation must not create a wire"
    monkeypatch.delenv("AGENT_COMMS_ROOT")
    route.unlink()
    assert resolve_comms_route().observe_root() == tmp_path / ".agent-comms"
    assert not (tmp_path / ".agent-comms").exists()


def test_service_factory_uses_selection_and_retains_private_pin(tmp_path, monkeypatch):
    _, root, root_id = route_fixture(tmp_path, monkeypatch)
    service = wire()
    assert service.root == root
    assert service.owners._private_nk_launch == PrivateNkLaunch(root, root_id, tmp_path, None)
    assert wire(root).owners._private_nk_launch is None


def test_route_record_retains_the_external_format_and_private_marker(tmp_path):
    tmp_path.chmod(0o700)
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    root_id = Comms(root).messaging.initialize_private_initial_protocol()
    selected = ActiveRoute(root, root_id, tmp_path)
    encoded = FieldCodec.encode(selected)
    assert encoded == {
        "version": 1, "root": str(root), "wire_root_id": root_id,
        "native_package": str(tmp_path),
    }
    route_file = tmp_path / "active-route.json"
    route_file.write_text(json.dumps(encoded))
    route_file.chmod(0o600)
    assert read_active_route(route_file) == selected
    assert read_active_route(route_file).observe_root() == root
    assert not (root / "registry.json").exists()


@pytest.mark.parametrize("change", [
    {"version": True}, {"version": "1"}, {"version": 2}, {"version": None},
    {"root": ""}, {"root": "relative"}, {"root": "/tmp/../private"}, {"root": None},
    {"native_package": 7}, {"native_package": "relative"},
    {"native_package": "/tmp/../package"}, {"wire_root_id": 9},
    {"wire_root_id": "A" * 32}, {"wire_root_id": "a" * 31}, {"unexpected": True},
])
def test_malformed_route_identity_is_denied_at_the_real_file_boundary(tmp_path, change):
    tmp_path.chmod(0o700)
    route_file = tmp_path / "active-route.json"
    value = FieldCodec.encode(ActiveRoute(tmp_path, "a" * 32, tmp_path))
    value.update(change)
    route_file.write_text(json.dumps(value))
    route_file.chmod(0o600)
    with pytest.raises(ValueError):
        read_active_route(route_file)
    assert not (tmp_path / "registry.json").exists()


@pytest.mark.parametrize("missing", ["version", "root", "wire_root_id", "native_package"])
def test_route_fields_remain_required_even_with_constructor_defaults(tmp_path, missing):
    value = FieldCodec.encode(ActiveRoute(tmp_path, "a" * 32, tmp_path))
    value.pop(missing)
    with pytest.raises(ValueError):
        ActiveRoute.from_record(value)
