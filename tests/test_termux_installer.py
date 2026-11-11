"""The full installer must replace resolved source builds, even at the same version."""
import json
import os
from pathlib import Path
import shutil
import subprocess


REPOSITORY = Path(__file__).resolve().parents[1]


def test_resolved_sources_are_reinstalled_without_rebuilding_native_packages(tmp_path: Path) -> None:
    home = tmp_path / "home"
    installation = home / ".local/share/toad-comms"
    source = installation / "source"
    stack = source / "stack"
    prefix = tmp_path / "prefix"
    commands = tmp_path / "commands"
    for directory in (source / ".git", stack / ".venv/bin", prefix / "bin", commands):
        directory.mkdir(parents=True)
    fixtures = Path(__file__).parent / "fixtures/termux_installer"
    for target in (prefix / "bin/python", stack / ".venv/bin/python"):
        shutil.copyfile(fixtures / "python.py", target)
        target.chmod(0o755)
    for target in (prefix / "bin/pkg", commands / "git", commands / "uv"):
        shutil.copyfile(fixtures / "noop.sh", target)
        target.chmod(0o755)
    launcher = source / "stack/bin/toad-comms-termux"
    launcher.parent.mkdir()
    shutil.copyfile(REPOSITORY / "stack/bin/toad-comms-termux", launcher)
    requirements = installation / "requirements.txt"
    requirements.write_text(
        "# A resolved foreign requirements file, including a conditional direct source.\n"
        "-e ..\n"
        "numpy==2.5.4\n"
        "batrachian-toad @ git+https://example.invalid/toad.git@abc123\n"
        "ui-theme @ git+https://example.invalid/theme.git@def456 ; python_version >= '3.14'\n"
    )
    subprocess.run(["bash", str(REPOSITORY / "tools/install-termux.sh")], check=True,
                   env={**os.environ, "HOME": str(home), "PREFIX": str(prefix),
                        "PATH": f"{prefix / 'bin'}:{commands}:{os.environ['PATH']}"},
                   capture_output=True, text=True)
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
