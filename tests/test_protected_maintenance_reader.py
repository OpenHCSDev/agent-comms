"""Disposable fake-identity checks; these do NOT prove host operator isolation."""

import json
import os
from pathlib import Path

import pytest

import agent_comms.protected_maintenance_reader as reader
from agent_comms.declarations import RelationViolationError
from agent_comms.maintenance_barrier import MaintenanceBarrier
from maintenance_control_fixture import FixtureMaintenanceControl

ROOT_ID = "a" * 32
NONCE = "b" * 32


def _put(path: Path, value: dict[str, object], mode: int) -> None:
    path.write_text(json.dumps(value))
    path.chmod(mode)


@pytest.fixture
def fake_protected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A fake owner/group under a disposable root; never root authentication."""
    config_base = tmp_path / "config"
    state_base = tmp_path / "state"
    config_base.mkdir(mode=0o755)
    state_base.mkdir(mode=0o755)
    directory = state_base / ROOT_ID
    directory.mkdir(mode=0o750)
    wire = tmp_path / "wire"
    wire.mkdir()
    monkeypatch.setattr(reader, "_CONFIG_BASE", config_base)
    monkeypatch.setattr(reader, "_STATE_BASE", state_base)
    monkeypatch.setattr(reader, "_TRUSTED_OWNER_UID", os.geteuid())
    monkeypatch.setattr(reader, "_CONFIG_OWNER_GID", os.getegid())
    monkeypatch.setattr(reader, "_ANCESTOR_STOP", tmp_path)
    config_path = config_base / f"{ROOT_ID}.json"
    config = {
        "version": 1,
        "required": True,
        "root": str(wire.resolve()),
        "root_id": ROOT_ID,
        "state_dir": str(directory),
        "reader_gid": os.getegid(),
    }
    gate = MaintenanceBarrier(wire / "registry.json", protected_config_path=config_path)
    return gate, config_path, config, directory


def _seed(gate: MaintenanceBarrier, config_path: Path, config: dict, directory: Path, phase: str):
    _put(config_path, config, 0o644)
    marker = {"version": 1, "root": config["root"], "root_id": ROOT_ID, "generation": 1}
    state = {**marker, "operator": "fake-operator", "nonce": NONCE, "phase": phase}
    _put(directory / "enabled", marker, 0o640)
    _put(directory / "state.json", state, 0o640)


def test_ordinary_default_off_api_unchanged(tmp_path: Path) -> None:
    gate = MaintenanceBarrier(tmp_path / "wire" / "registry.json")
    assert gate.read() is None
    assert not hasattr(gate, "begin") and not hasattr(gate, "reopen")


def test_configured_missing_never_falls_back_to_user_witness(fake_protected) -> None:
    gate, config_path, config, directory = fake_protected
    legacy = MaintenanceBarrier(gate.registry_path)
    FixtureMaintenanceControl(legacy).begin("fake-fixture")
    # Disposable forged legacy READY makes a fallback visibly wrong.
    legacy_state = json.loads(legacy.state_path.read_text())
    legacy_state["phase"] = "ready"
    _put(legacy.state_path, legacy_state, 0o600)
    legacy.assert_open_unlocked()
    with pytest.raises(RelationViolationError, match="protected witness"):
        gate.assert_open_unlocked()  # A missing protected config never reads legacy READY.
    _put(config_path, config, 0o644)
    with pytest.raises(RelationViolationError, match="protected witness"):
        gate.assert_open_unlocked()  # Both protected witness files absent.
    _seed(gate, config_path, config, directory, "paused")
    with pytest.raises(RelationViolationError, match="Maintenance admission closed"):
        gate.assert_open_unlocked()
    _seed(gate, config_path, config, directory, "ready")
    assert gate.read().phase == "ready"
    with gate.admit_ingress() as receipt:
        assert receipt is not None and receipt.phase == "ready"


@pytest.mark.parametrize(
    "fault",
    [
        "missing-marker", "missing-state", "symlink", "writable", "wrong-group",
        "mismatch", "bad-config", "bad-parent",
    ],
)
def test_protected_reader_closes_on_damage(fake_protected, fault: str) -> None:
    gate, config_path, config, directory = fake_protected
    _seed(gate, config_path, config, directory, "ready")
    marker = directory / "enabled"
    state = directory / "state.json"
    if fault == "missing-marker":
        marker.unlink()
    elif fault == "missing-state":
        state.unlink()
    elif fault == "symlink":
        state.unlink()
        state.symlink_to(marker)
    elif fault == "writable":
        state.chmod(0o660)
    elif fault == "wrong-group":
        # Cannot chgrp in an unprivileged test; a config group mismatch closes.
        config["reader_gid"] = os.getegid() + 10000
        _put(config_path, config, 0o644)
    elif fault == "mismatch":
        content = json.loads(state.read_text())
        content["generation"] = 2
        _put(state, content, 0o640)
    elif fault == "bad-config":
        config_path.chmod(0o666)
    else:
        directory.chmod(0o770)
    with pytest.raises(RelationViolationError, match="protected witness"):
        gate.read()


def test_config_root_and_trust_anchor_are_not_caller_fallback(fake_protected) -> None:
    gate, config_path, config, directory = fake_protected
    _seed(gate, config_path, config, directory, "ready")
    config["root"] = str(directory)
    _put(config_path, config, 0o644)
    with pytest.raises(RelationViolationError, match="protected witness"):
        gate.read()
    config["root"] = str(gate.registry_path.parent.resolve())
    _put(config_path, config, 0o644)
    other = gate.registry_path.parent / "evil.json"
    other.write_text(config_path.read_text())
    other.chmod(0o644)
    with pytest.raises(RelationViolationError, match="protected witness"):
        MaintenanceBarrier(gate.registry_path, protected_config_path=other).read()
