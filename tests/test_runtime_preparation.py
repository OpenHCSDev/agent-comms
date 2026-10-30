"""The target probe loads actual distribution metadata, not a patched loader."""

from pathlib import Path

import pytest

from agent_comms.installer.probes.packages import PreparePackageProbe
from agent_comms.runtime_preparation import RuntimePreparation


class PreparationEntered(RuntimeError):
    pass


class DeclaredPreparation(RuntimePreparation):
    def prepare(self) -> None:
        raise PreparationEntered("distribution-owned preparation ran")


@pytest.fixture
def preparation_metadata(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    metadata = tmp_path / "p928_preparation-1.0.dist-info"
    metadata.mkdir()
    (metadata / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: p928-preparation\nVersion: 1.0\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    return metadata / "entry_points.txt"


def test_target_probe_runs_the_distribution_declaration(preparation_metadata: Path):
    implementation = f"{DeclaredPreparation.__module__}:{DeclaredPreparation.__qualname__}"
    preparation_metadata.write_text(
        f"[{RuntimePreparation.entry_point_group}]\nrequirement = {implementation}\n"
    )
    with pytest.raises(PreparationEntered, match="distribution-owned preparation ran"):
        PreparePackageProbe("p928-preparation").run()


@pytest.mark.parametrize("value", [object(), object, RuntimePreparation])
def test_foreign_preparation_must_be_a_concrete_nominal_class(value: object):
    with pytest.raises(TypeError):
        RuntimePreparation.decode(value)


@pytest.mark.parametrize("entries", ["", "first = builtins:object\nsecond = builtins:object\n"])
def test_declared_preparation_requires_exactly_one_entry_point(
    preparation_metadata: Path, entries: str,
):
    preparation_metadata.write_text(f"[{RuntimePreparation.entry_point_group}]\n{entries}")
    with pytest.raises(ValueError, match="expected one"):
        RuntimePreparation.load("p928-preparation")


def test_entry_point_shape_does_not_admit_an_unrelated_class(preparation_metadata: Path):
    preparation_metadata.write_text(
        f"[{RuntimePreparation.entry_point_group}]\nrequirement = builtins:object\n"
    )
    with pytest.raises(TypeError, match="must implement"):
        RuntimePreparation.load("p928-preparation")
