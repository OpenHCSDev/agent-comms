import hashlib
import json
import os
import sys
import tomllib
from dataclasses import dataclass, fields
from importlib.metadata import PathDistribution
from pathlib import Path
from zipfile import ZipFile

import pytest

from agent_comms.nominal.field_codec import FieldCodec
from agent_comms.installer.refusals import UpgradeRefused
from agent_comms.installer.commands import InstallCommands
from agent_comms.installer.probes.interpreter import InterpreterBuildProbe
from agent_comms.installer.probes.packages import (
    ArchivePin, ArchiveRecord, GitPin, InstalledPackagesProbe, PackageRecord,
)
from agent_comms.installer.runtime_resolution import (
    CorePackage, ResolvedLock, RuntimeResolution, ToadPackage,
)
from agent_comms.installer.runtime_stack import RuntimeStack
from agent_comms.launch_runtime import LaunchRuntime


def test_package_probe_entry_uses_one_declaration_before_refusing_build_identity():
    runtime = LaunchRuntime.running()
    with InstallCommands() as commands, pytest.raises(
            UpgradeRefused, match="Core is editable|wheel stamp does not name"):
        commands.probe(
            InstalledPackagesProbe("not-a-built-wheel"), runtime.interpreter,
            env=runtime.environment(os.environ),
        )


@dataclass(frozen=True)
class InstalledWheel:
    runtime: LaunchRuntime
    wheel: Path
    distribution: PathDistribution
    source: ArchiveRecord

@pytest.fixture
def installed_wheel(tmp_path: Path) -> InstalledWheel:
    prefix = tmp_path / "runtime"
    purelib = LaunchRuntime.at_prefix(prefix).purelib
    metadata = "sample-1.0.dist-info"
    contents = {
        "sample/__init__.py": b"value = 1\n",
        f"{metadata}/METADATA": b"Metadata-Version: 2.4\nName: sample\nVersion: 1.0\n",
        f"{metadata}/WHEEL": b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        f"{metadata}/RECORD": b"installer replaces this record\n",
    }
    wheel = tmp_path / "sample-1.0-py3-none-any.whl"
    with ZipFile(wheel, "w") as archive:
        for name, data in contents.items():
            archive.writestr(name, data)
            target = purelib / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
    source = ArchiveRecord(url=wheel.as_uri())
    (purelib / metadata / "direct_url.json").write_text(json.dumps({
        "url": source.url, "archive_info": {},
    }))
    return InstalledWheel(LaunchRuntime.at_prefix(prefix), wheel,
                          PathDistribution(purelib / metadata), source)


def test_verified_archive_preserves_content_without_inventing_git(installed_wheel: InstalledWheel):
    original = installed_wheel.distribution.read_text("direct_url.json")
    verified = installed_wheel.source.verified_wheel(
        installed_wheel.wheel, installed_wheel.distribution, installed_wheel.runtime
    )
    assert verified.archive_info.hashes == {
        "sha256": hashlib.sha256(installed_wheel.wheel.read_bytes()).hexdigest()
    }
    assert verified.wheel_inputs() == (installed_wheel.wheel,)
    assert installed_wheel.distribution.read_text("direct_url.json") == original
    with pytest.raises(ValueError, match="Git revision"):
        verified.revision()
    record = RuntimeResolution.model_validate({
        "core_commit": "unqualified-fixture",
        "packages": {"sample": {"version": "1.0", "source": verified.model_dump()}},
    })
    assert RuntimeResolution.model_validate_json(record.model_dump_json()) == record


@pytest.mark.parametrize("changed", [False, True])
def test_donor_wheel_selection_uses_its_recorded_prefix(
    installed_wheel: InstalledWheel, monkeypatch: pytest.MonkeyPatch, changed: bool
):
    """Model the invoked prefix; this does not launch or qualify a donor worker."""
    original = installed_wheel.distribution.read_text("direct_url.json")
    verified = installed_wheel.source.verified_wheel(
        installed_wheel.wheel, installed_wheel.distribution, installed_wheel.runtime
    )
    recorded = RuntimeResolution.model_validate({
        "core_commit": "unqualified-fixture",
        "packages": {"sample": {"version": "1.0", "source": verified.model_dump()}},
    })
    recorded.save(installed_wheel.runtime.prefix)
    # sys.executable is the foreign launch input; no production method is patched.
    monkeypatch.setattr(sys, "executable", str(installed_wheel.runtime.interpreter))
    probe = InstalledPackagesProbe(recorded.core_commit)
    assert tuple(field.name for field in fields(probe)) == ("core",)
    assert probe.wheel_inputs() == recorded.wheel_inputs() == (installed_wheel.wheel,)
    assert original is not None
    raw = ArchiveRecord.model_validate_json(original)
    if changed:
        installed_wheel.distribution.locate_file("sample/__init__.py").write_bytes(b"changed\n")
        with pytest.raises(ValueError, match="Installed bytes differ"):
            raw.verified_wheel(probe.wheel_inputs()[0], installed_wheel.distribution,
                               installed_wheel.runtime)
    else:
        assert raw.verified_wheel(probe.wheel_inputs()[0], installed_wheel.distribution,
                                  installed_wheel.runtime) == verified
    assert installed_wheel.distribution.read_text("direct_url.json") == original


def test_runtime_stack_reobserves_its_resolution_wheel_inputs(installed_wheel: InstalledWheel):
    with InstallCommands() as commands:
        stack = RuntimeStack(Path(__file__).resolve().parents[1], commands,
                             runtime=installed_wheel.runtime)
        assert stack.wheel_inputs() == ()
        verified = installed_wheel.source.verified_wheel(
            installed_wheel.wheel, installed_wheel.distribution, installed_wheel.runtime
        )
        recorded = RuntimeResolution(
            core_commit="unqualified-fixture",
            packages={"sample": PackageRecord(version="1.0", source=verified)},
        )
        recorded.save(installed_wheel.runtime.prefix)
        assert stack.wheel_inputs() == (installed_wheel.wheel,)


def test_changed_installed_member_refuses_archive_proof(installed_wheel: InstalledWheel):
    installed_wheel.distribution.locate_file("sample/__init__.py").write_bytes(b"value = 2\n")
    with pytest.raises(ValueError, match="Installed bytes differ"):
        installed_wheel.source.verified_wheel(
            installed_wheel.wheel, installed_wheel.distribution, installed_wheel.runtime
        )


def test_root_wheel_domain_does_not_silently_skip_data_members(installed_wheel: InstalledWheel):
    with ZipFile(installed_wheel.wheel, "a") as archive:
        archive.writestr("sample-1.0.data/scripts/other", b"unverified\n")
    with pytest.raises(ValueError, match="root-layout"):
        installed_wheel.source.verified_wheel(
            installed_wheel.wheel, installed_wheel.distribution, installed_wheel.runtime
        )


def test_archive_proof_binds_both_origin_and_runtime(installed_wheel: InstalledWheel, tmp_path: Path):
    wrong_origin = ArchiveRecord(url=(tmp_path / "different.whl").as_uri())
    with pytest.raises(ValueError, match="origin differs"):
        wrong_origin.verified_wheel(installed_wheel.wheel, installed_wheel.distribution,
                                    installed_wheel.runtime)
    with pytest.raises(ValueError, match="escapes its runtime"):
        installed_wheel.source.verified_wheel(
            installed_wheel.wheel, installed_wheel.distribution,
            LaunchRuntime.at_prefix(tmp_path / "unrelated"),
        )


def test_runtime_identity_refuses_missing_and_changed_interpreter():
    with pytest.raises(ValueError, match="no recorded interpreter"):
        manifest().require_runtime(manifest())
    observed = InterpreterBuildProbe().run()
    first = manifest().model_copy(update={"interpreter": observed})
    assert RuntimeResolution.model_validate_json(first.model_dump_json()) == first
    first.require_runtime(first)
    changed_image = observed.artifacts[0].model_copy(update={"sha256": "0" * 64})
    second = first.model_copy(update={"interpreter": observed.model_copy(update={
        "artifacts": (changed_image, *observed.artifacts[1:]),
    })})
    with pytest.raises(UpgradeRefused, match="recorded build inputs"):
        first.require_runtime(second)


TOAD_COMMIT = "a" * 40
TEXTUAL_COMMIT = "b" * 40


def manifest(core="core", toad=TOAD_COMMIT, wheel="old-wheel"):
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
                        "vcs_info": {"commit_id": TEXTUAL_COMMIT},
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
                    "source": {"git": f"https://example/toad#{TOAD_COMMIT}"},
                },
                {
                    "name": "textual",
                    "version": "3",
                    "source": {"git": f"https://example/textual#{TEXTUAL_COMMIT}"},
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
    assert manifest(toad="d" * 40).pins() != pins
    assert lock().pins({**selected, "unselected": "5"}, "core") != built.pins()


@pytest.mark.parametrize("digest", ["0" * 64, "1" * 64])
def test_explicit_archive_keeps_its_pin_and_refuses_repository_ancestry(digest: str):
    built = manifest()
    archive = ArchiveRecord.model_validate({
        "url": "file:///owned/inputs/batrachian_toad-2-py3-none-any.whl",
        "archive_info": {"hashes": {"sha256": digest}},
    })
    built = built.model_copy(update={"packages": {
        **built.packages,
        ToadPackage.distribution: PackageRecord(version="2", source=archive),
    }})
    pins = built.pins()
    assert pins[CorePackage.distribution] == GitPin("1", "core")
    assert pins[ToadPackage.distribution] == ArchivePin(
        "2", archive.url, (("sha256", digest),)
    )
    selected = {name: record.version for name, record in built.packages.items()}
    assert pins != lock().pins(selected, built.core_commit)
    with pytest.raises(ValueError, match="Git revision"):
        ToadPackage.commit(built)


def test_missing_and_ambiguous_lock_evidence_refuses():
    with pytest.raises(ValueError):
        lock().pins({"missing": "1"}, "core")
    ambiguous = lock()
    ambiguous.package.append(ambiguous.package[0])
    with pytest.raises(ValueError):
        ambiguous.pins({"agent-comms": "1"}, "core")


def test_the_stack_relocks_every_branch_tracking_package_and_refuses_a_unit_branch():
    """resolve() upgraded only stack/pyproject.toml's sources, so transitive metaclass-registry
    stayed at fb7c871 before MroDispatch; Toad pinned to work/unit-toad--P694 after its merge
    relocked the same branch head while InstallDue saw main ahead."""
    path = Path(__file__).resolve().parents[1] / "stack" / "uv.lock"
    lock = ResolvedLock.read(path)
    tracked = lock.tracked()
    assert {"batrachian-toad", "metaclass-registry", "textual"} <= set(tracked)
    assert "objectstate" not in tracked
    document = tomllib.loads(path.read_text())
    toad = next(entry for entry in document["package"] if entry["name"] == "batrachian-toad")
    toad["source"]["git"] = toad["source"]["git"].replace("branch=main", "branch=work/unit-toad--P694")
    with pytest.raises(ValueError, match="batrachian-toad"):
        ResolvedLock.model_validate(document).tracked()
