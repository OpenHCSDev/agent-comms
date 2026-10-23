"""The checkout command runs a real native turn without publishing its private build."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from agent_comms.installer import practice
from agent_comms.installer.backup import SavedFile
from agent_comms.installer.refusals import UpgradeRefused
from agent_comms.installer.commands import InstallCommands
from agent_comms.installer.declared_uv import DeclaredUv
from agent_comms.installer.upgrade import Stack
from agent_comms.launch_runtime import LaunchRuntime
from agent_comms.repo import Checkout


def test_rehearsal_home_requires_both_installed_inputs(tmp_path, monkeypatch, test_uv):
    executable = practice.TestHome(tmp_path).uv
    installed_home = tmp_path / "installed"
    installed_home.mkdir()
    monkeypatch.setenv("HOME", str(installed_home))
    home = practice.RehearsalHome(tmp_path / "rehearsal")
    with pytest.raises(FileNotFoundError):
        home.prepare()
    selected = installed_home / "runtime"
    selected.mkdir()
    current = LaunchRuntime.current_pointer()
    current.parent.mkdir(parents=True)
    current.symlink_to(selected, target_is_directory=True)
    with pytest.raises(FileNotFoundError):
        home.prepare()
    uv = practice.RuntimeHome.uv_path(installed_home)
    uv.parent.mkdir(parents=True)
    uv.symlink_to(executable)
    home.prepare()
    assert (home.path / current.relative_to(installed_home)).resolve() == selected
    assert (home.path / uv.relative_to(installed_home)).resolve() == executable.resolve()


def test_test_home_requires_uv_on_path_even_with_an_installer_uv(tmp_path, monkeypatch, test_uv):
    executable = practice.TestHome(tmp_path).uv
    installed_home = tmp_path / "installed"
    uv = practice.RuntimeHome.uv_path(installed_home)
    uv.parent.mkdir(parents=True)
    uv.symlink_to(executable)
    monkeypatch.setenv("HOME", str(installed_home))
    monkeypatch.setenv("PATH", "")
    with pytest.raises(UpgradeRefused):
        practice.TestHome(tmp_path / "test-home").prepare()


@pytest.mark.timeout(290)
def test_checkout_native_tests_use_a_private_runtime(tmp_path, test_uv):
    repository = Path(__file__).resolve().parents[1]
    checkout = tmp_path / "checkout"
    subprocess.run(["git", "clone", "--shared", "-q", repository, checkout], check=True)
    Checkout(checkout).prepare()

    # A machine's existing installation is not the test command's build destination.
    live_home = tmp_path / "live-home"
    practice.TestHome(live_home).prepare()
    assert not (live_home / ".local/share/agent-comms/current").exists(follow_symlinks=False)
    runtimes = live_home / ".local/share/agent-comms/runtimes"
    runtimes.mkdir(parents=True)
    retained = runtimes / "retained"
    retained.mkdir()
    witness = retained / "keep"
    witness.write_text("Existing installation")
    current = live_home / ".local/share/agent-comms/current"
    current.unlink(missing_ok=True)
    current.symlink_to(retained, target_is_directory=True)
    selected = current.readlink()
    before = {
        path.relative_to(live_home): SavedFile.at(path)
        for path in (runtimes, retained, witness, current)
    }
    bootstrap = Stack(repository, InstallCommands(), home=live_home,
                      runtime=LaunchRuntime.running())
    cache = subprocess.run(
        [bootstrap.uv, "cache", "dir"], check=True, capture_output=True, text=True
    ).stdout.strip()
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "agent_comms.installer.test_runtime",
            "tests/test_native_input_bus_path.py",
        ],
        cwd=checkout,
        env={
            **os.environ,
            "HOME": str(live_home),
            "PYTHONPATH": str(checkout / "src"),
            "UV_CACHE_DIR": cache,
        },
        capture_output=True,
        text=True,
        timeout=280,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert set(runtimes.iterdir()) == {retained}
    assert {path: SavedFile.at(live_home / path) for path in before} == before
    assert current.readlink() == selected
    assert witness.read_text() == "Existing installation"
    assert not tuple((live_home / ".cache/agent-scratch").iterdir())
    print(result.stdout)


def test_the_installer_replaces_a_uv_its_checkout_does_not_declare(tmp_path, test_uv):
    """A uv reporting another version is replaced by the checkout's declared uv, in place."""
    checkout = Path(__file__).resolve().parents[1]
    uv = practice.RuntimeHome.uv_path(tmp_path)
    uv.parent.mkdir(parents=True)
    uv.write_text(f'#!/bin/sh\n[ "$1" = --version ] && echo "uv 0.0.0" && exit\n'
                  f'exec {test_uv} "$@"\n')
    uv.chmod(0o700)
    with InstallCommands() as commands:
        stack = Stack(checkout, commands, tmp_path, runtime=LaunchRuntime.running())
        stack.require_uv()
        declared = DeclaredUv.of(checkout / "pyproject.toml", uv)
        assert declared.accepts(commands.run(uv, "--version"))
        assert not uv.is_symlink() and not uv.read_bytes().startswith(b"#!")
        assert [path.name for path in uv.parent.iterdir()] == ["uv"]
