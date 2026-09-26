"""Explicit production ACP/worker N/K flags: no inferred activation or provider call."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from agent_comms import acp, cohort_foreground, private_nk_entrypoint, worker
from agent_comms.coordination_store import IdentityConflict, PublicationActivationBlocked
from agent_comms.private_nk_entrypoint import PACKAGE_ENV, ROOT_ID_ENV, private_nk_launch
from test_native_prompt_binding import _root
from test_native_prompt_binding import tmp_path as private_root_fixture

tmp_path = private_root_fixture


def test_explicit_owner_entrypoint_requires_exact_root_and_package(tmp_path, monkeypatch):
    root, root_id, _, _, _ = _root(tmp_path)
    monkeypatch.setattr(cohort_foreground, "_trusted_package", lambda _: None)
    assert private_nk_launch(root, {}) is None  # marker alone never enables a worker
    for values in (
        {ROOT_ID_ENV: root_id},
        {PACKAGE_ENV: str(tmp_path)},
        {ROOT_ID_ENV: "f" * 32, PACKAGE_ENV: str(tmp_path)},
        {ROOT_ID_ENV: root_id, PACKAGE_ENV: "relative-package"},
        {ROOT_ID_ENV: root_id, PACKAGE_ENV: str(tmp_path), "PI_PROMPT": "legacy"},
    ):
        with pytest.raises((PublicationActivationBlocked, IdentityConflict, ValueError)):
            private_nk_launch(root, values)
    exact = private_nk_launch(root, {ROOT_ID_ENV: root_id, PACKAGE_ENV: str(tmp_path)})
    assert exact is not None
    assert exact.wire_root_id == root_id and exact.native_package == tmp_path
    assert not (root / "native-sessions").exists()


async def test_worker_rejects_partial_private_configuration_before_wire(tmp_path, monkeypatch):
    called = False

    def wire_would_create_root():
        nonlocal called
        called = True
        raise AssertionError("wire should not be constructed")

    monkeypatch.setattr(worker, "wire", wire_would_create_root)
    monkeypatch.setenv("AGENT_COMMS_ROOT", str(tmp_path / "absent"))
    monkeypatch.setenv(ROOT_ID_ENV, "f" * 32)
    monkeypatch.delenv(PACKAGE_ENV, raising=False)
    with pytest.raises(PublicationActivationBlocked, match="exact root ID"):
        await worker.run()
    assert not called and not (tmp_path / "absent").exists()


def test_stdio_acp_refuses_partial_private_configuration_before_wire(tmp_path, monkeypatch):
    called = False

    def wire_would_create_root():
        nonlocal called
        called = True
        raise AssertionError("wire should not be constructed")

    monkeypatch.setattr(acp, "wire", wire_would_create_root)
    monkeypatch.setenv("AGENT_COMMS_ROOT", str(tmp_path / "absent"))
    monkeypatch.setenv(ROOT_ID_ENV, "f" * 32)
    monkeypatch.delenv(PACKAGE_ENV, raising=False)
    monkeypatch.setattr(sys, "argv", ["agent_comms.acp"])
    with pytest.raises(PublicationActivationBlocked, match="exact root ID"):
        acp.main()
    assert not called and not (tmp_path / "absent").exists()


def test_environment_launch_uses_selected_root_not_cwd(tmp_path, monkeypatch):
    root, root_id, _, _, _ = _root(tmp_path)
    seen: list[Path] = []

    def trusted(package: Path):
        seen.append(package)

    monkeypatch.setattr(cohort_foreground, "_trusted_package", trusted)
    monkeypatch.setenv("AGENT_COMMS_ROOT", str(root))
    monkeypatch.setenv(ROOT_ID_ENV, root_id)
    monkeypatch.setenv(PACKAGE_ENV, str(tmp_path))
    monkeypatch.chdir("/")
    selected = private_nk_entrypoint.private_nk_from_environment()
    assert selected is not None and selected.wire_root_id == root_id
    assert selected.native_package == tmp_path and seen == [tmp_path]
    assert os.environ["AGENT_COMMS_ROOT"] == str(root)
