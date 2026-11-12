"""Both frontend installs share updates and launchers, with host-specific dependency work."""
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest


REPOSITORY = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures/termux_installer"


@pytest.fixture
def installer(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    source = home / ".local/share/toad-comms/source"
    stack = source / "stack"
    commands = home / ".local/bin"
    for directory in (source / ".git", stack / ".venv/bin", commands):
        directory.mkdir(parents=True)
    python = stack / ".venv/bin/python"
    shutil.copyfile(FIXTURES / "python.py", python)
    python.chmod(0o755)
    for name in ("git", "uv", "sudo", "curl", "apt-get"):
        command = commands / name
        shutil.copyfile(FIXTURES / "command.py", command)
        command.chmod(0o755)
    shutil.copyfile(REPOSITORY / "tools/install-toad.sh", commands / "install-toad.sh")
    launcher = source / "stack/bin/toad-local"
    launcher.parent.mkdir()
    shutil.copyfile(REPOSITORY / "stack/bin/toad-local", launcher)
    return home


def run_installer(home: Path, script: str, **environment: str) -> None:
    env = {**os.environ, "HOME": str(home),
           "PATH": f"{home / 'prefix/bin'}:{home / '.local/bin'}:{os.environ['PATH']}"}
    env.pop("PREFIX", None)
    subprocess.run(["bash", str(REPOSITORY / "tools" / script)], check=True,
                   env={**env, **environment}, capture_output=True, text=True)


def test_resolved_sources_are_reinstalled_without_rebuilding_native_packages(installer: Path) -> None:
    home = installer
    installation = home / ".local/share/toad-comms"
    stack = installation / "source/stack"
    prefix = home / "prefix"
    (prefix / "bin").mkdir(parents=True)
    for name, fixture in (("python", "python.py"), ("pkg", "noop.sh"), ("uv", "command.py")):
        command = prefix / "bin" / name
        shutil.copyfile(FIXTURES / fixture, command)
        command.chmod(0o755)
    requirements = installation / "requirements.txt"
    requirements.write_text(
        "# A resolved foreign requirements file, including a conditional direct source.\n"
        "-e ..\n"
        "numpy==2.5.4\n"
        "batrachian-toad @ git+https://example.invalid/toad.git@abc123\n"
        "ui-theme @ git+https://example.invalid/theme.git@def456 ; python_version >= '3.14'\n"
    )
    # Exercise the already-published Termux URL's entry point, not just its implementation.
    run_installer(home, "install-termux.sh", PREFIX=str(prefix))
    calls = [json.loads(line) for line in (stack / "installer-calls.jsonl").read_text().splitlines()]
    installs = [call for call in calls if call[:3] == ["-m", "pip", "install"]]
    forced = [call for call in installs if "--force-reinstall" in call]
    assert len(forced) == 1, installs
    direct = Path(forced[0][forced[0].index("-r") + 1]).read_text()
    assert "batrachian-toad @ git+" in direct
    assert "ui-theme @ git+" in direct
    assert "numpy" not in direct
    regular, = [call for call in installs if "--force-reinstall" not in call]
    indexed = Path(regular[regular.index("-r") + 1]).read_text()
    assert "numpy==2.5.4" in indexed
    assert "-e .." in indexed
    assert " @ " not in indexed
    assert (prefix / "bin/toad-comms").resolve() == installation / "bin/toad-comms"
    assert (home / ".shortcuts/Toad Comms").resolve() == installation / "bin/toad-comms"


def test_debian_uses_the_declared_linux_stack_and_publishes_commands(installer: Path) -> None:
    run_installer(installer, "install-toad.sh")
    calls = [json.loads(line) for line in (installer / "commands.jsonl").read_text().splitlines()]
    assert ["uv", "sync", "--project", ".", "--upgrade"] in calls
    assert ["uv", "pip", "check", "--python", ".venv/bin/python"] in calls
    assert ["sudo", "apt-get", "update"] in calls
    destination = installer / ".local/share/toad-comms/bin/toad-comms"
    assert ["sudo", "ln", "-sfn", str(destination), "/usr/local/bin/toad-comms"] in calls
    assert any(call[-1] == "/usr/local/bin/toad" for call in calls)
    assert not (installer / ".shortcuts").exists()
    assert destination.is_file()
    assert "termux-user-repository" not in json.dumps(calls)
