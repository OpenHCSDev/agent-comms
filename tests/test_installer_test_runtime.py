"""The checkout command runs a real native turn without publishing its private build."""

from __future__ import annotations

import os
import subprocess
import sys
import tarfile
from io import BytesIO
from pathlib import Path

import pytest

from agent_comms.installer.backup import SavedFile
from agent_comms.installer.practice import prepare_runtime_home
from agent_comms.installer.upgrade import Stack
from agent_comms.launch_runtime import LaunchRuntime


@pytest.mark.timeout(290)
def test_checkout_native_tests_use_a_private_runtime(tmp_path):
    repository = Path(__file__).resolve().parents[1]
    checkout = tmp_path / "checkout"
    archive = subprocess.run(
        ["git", "-C", repository, "archive", "HEAD"], check=True, capture_output=True
    ).stdout
    with tarfile.open(fileobj=BytesIO(archive)) as tree:
        tree.extractall(checkout, filter="data")
    subprocess.run(["git", "init", "-q", checkout], check=True)
    subprocess.run(["git", "-C", checkout, "add", "."], check=True)
    # Archives omit submodule entries; retain their declarations for Checkout.prepare.
    entries = subprocess.run(
        ["git", "-C", repository, "ls-files", "--stage"], check=True, capture_output=True, text=True
    ).stdout
    subprocess.run(
        ["git", "-C", checkout, "update-index", "--index-info"],
        check=True,
        input="".join(line + "\n" for line in entries.splitlines() if line.startswith("160000 ")),
        text=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            checkout,
            "-c",
            "user.name=Runtime test",
            "-c",
            "user.email=runtime@example.invalid",
            "commit",
            "-qm",
            "Fixture checkout",
        ],
        check=True,
    )

    # A machine's existing installation is not the test command's build destination.
    live_home = tmp_path / "live-home"
    prepare_runtime_home(live_home)
    runtimes = live_home / ".local/share/agent-comms/runtimes"
    runtimes.mkdir()
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
    bootstrap = Stack(repository, home=live_home, runtime=LaunchRuntime.running())
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
