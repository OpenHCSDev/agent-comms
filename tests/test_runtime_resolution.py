import os
import tomllib
from pathlib import Path

import pytest
from pydantic import ValidationError

from agent_comms.field_codec import FieldCodec
from agent_comms.installer.refusals import UpgradeRefused
from agent_comms.installer.commands import InstallCommands
from agent_comms.installer.probes.packages import InstalledPackagesProbe
from agent_comms.installer.runtime_resolution import ResolvedLock, RuntimeResolution, StackProject
from agent_comms.launch_runtime import LaunchRuntime


def test_package_probe_entry_uses_one_declaration_before_refusing_build_identity():
    runtime = LaunchRuntime.running()
    with InstallCommands() as commands, pytest.raises(
            UpgradeRefused, match="Core is editable|wheel stamp does not name"):
        commands.probe(
            InstalledPackagesProbe("not-a-built-wheel"), runtime.interpreter,
            env=runtime.environment(os.environ),
        )


def manifest(core="core", toad="toad", wheel="old-wheel"):
    return RuntimeResolution.model_validate(
        {
            "core_commit": core,
            "packages": {
                "agent-comms": {"version": "1", "source": {"url": wheel, "archive_info": {}}},
                "batrachian-toad": {
                    "version": "2",
                    "source": {"url": "https://example/toad", "vcs_info": {"commit_id": toad}},
                },
                "textual": {
                    "version": "3",
                    "source": {
                        "url": "https://example/textual",
                        "vcs_info": {"commit_id": "textual"},
                    },
                },
                "rich": {"version": "4", "source": None},
            },
        }
    )


def test_installed_package_probe_transports_the_declared_records_and_source_cases():
    original = manifest().packages
    probe = InstalledPackagesProbe("core")
    decoded = FieldCodec.decode(probe.answer_type(), FieldCodec.encode(original, probe.answer_type()))
    assert decoded == original
    assert {name: record.pin() for name, record in decoded.items()} == {
        name: record.pin() for name, record in original.items()
    }


def lock():
    return ResolvedLock.model_validate(
        {
            "package": [
                {"name": "agent-comms", "source": {"editable": ".."}},
                {
                    "name": "batrachian-toad",
                    "version": "2",
                    "source": {"git": "https://example/toad#toad"},
                },
                {
                    "name": "textual",
                    "version": "3",
                    "source": {"git": "https://example/textual#textual"},
                },
                {"name": "rich", "version": "4", "source": {"registry": "https://pypi.org/simple"}},
                # The universal lock contains entries not selected by this synced environment.
                {
                    "name": "unselected",
                    "version": "5",
                    "source": {"registry": "https://pypi.org/simple"},
                },
            ]
        }
    )


def test_recorded_sources_match_selected_lock_not_wheel_locations():
    built = manifest()
    selected = {name: record.version for name, record in built.packages.items()}
    pins = lock().pins(selected, "core")
    assert built.pins() == pins
    assert manifest(wheel="another-wheel").pins() == pins
    assert manifest(core="changed").pins() != pins
    assert manifest(toad="changed").pins() != pins
    assert lock().pins({**selected, "unselected": "5"}, "core") != built.pins()


def test_missing_and_ambiguous_lock_evidence_refuses():
    with pytest.raises(ValueError):
        lock().pins({"missing": "1"}, "core")
    ambiguous = lock()
    ambiguous.package.append(ambiguous.package[0])
    with pytest.raises(ValueError):
        ambiguous.pins({"agent-comms": "1"}, "core")


def test_stack_refresh_includes_sources_declared_by_its_local_core():
    root = Path(__file__).resolve().parents[1]
    stack = StackProject.read(root / "stack")
    core = StackProject.read(root)
    assert set(stack.refresh_names(root / "stack")) == (
        set(stack.tool.uv.sources) | set(core.tool.uv.sources)
    )


def test_the_stack_tracks_the_main_install_due_measures_and_refuses_a_unit_branch():
    """Toad pinned to work/unit-toad--P694 after its merge: every install relocked the same
    branch head while InstallDue saw main ahead, and launched the same install again."""
    path = Path(__file__).resolve().parents[1] / "stack" / "pyproject.toml"
    project = tomllib.loads(path.read_text())
    StackProject.model_validate(project)
    project["tool"]["uv"]["sources"]["batrachian-toad"]["branch"] = "work/unit-toad--P694"
    with pytest.raises(ValidationError, match="batrachian-toad"):
        StackProject.model_validate(project)
