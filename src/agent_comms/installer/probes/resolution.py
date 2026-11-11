"""One target-owned observation of package inputs and the interpreter running them."""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path

from packaging.version import Version

import agent_comms
from agent_comms.execution.launch_runtime import LaunchRuntime
from agent_comms.installer.interpreter_operation import RuntimeProbe, TargetProbe
from agent_comms.installer.probes.interpreter import InterpreterBuildProbe
from agent_comms.installer.probes.packages import DistributionProbe, PackageRecord
from agent_comms.installer.runtime_resolution import ProjectMetadata, RuntimeResolution
from agent_comms.package_stamp import STAMP


@dataclass(frozen=True)
class InstalledPackagesProbe(DistributionProbe, RuntimeProbe[dict[str, PackageRecord]]):
    core: str

    def wheel_inputs(self) -> tuple[Path, ...]:
        """A built donor re-observes its own inputs without changing the request wire."""
        return RuntimeResolution.at(LaunchRuntime.running().prefix).wheel_inputs()

    def run(self) -> dict[str, PackageRecord]:
        if not Path(agent_comms.__file__).resolve().is_relative_to(Path(sys.prefix).resolve()):
            raise ValueError("Core is editable, not a built runtime package")
        actual = STAMP.commit()
        if actual != self.core:
            raise ValueError(
                "The wheel stamp does not name the selected source: "
                f"installed {actual}, requested {self.core}"
            )
        selected = self.distributions()
        records = {
            name: PackageRecord.from_wire(
                {
                    "version": dist.version,
                    "source": (
                        json.loads(source)
                        if (source := dist.read_text("direct_url.json"))
                        else None
                    ),
                }
            )
            for name, dist in selected.items()
        }
        for wheel in self.wheel_inputs():
            metadata = ProjectMetadata.from_wheel(wheel)
            record = records[metadata.name]
            if Version(record.version) != metadata.version:
                raise ValueError(f"Installed version differs from supplied wheel: {wheel}")
            records[metadata.name] = record.model_copy(update={
                "source": record.source.verified_wheel(
                    wheel, selected[metadata.name], LaunchRuntime.running()
                )
            })
        return records


@dataclass(frozen=True)
class WheelPackagesProbe(InstalledPackagesProbe):
    """Unrecorded targets supply inputs before their first resolution can be saved."""

    wheels: tuple[Path, ...]

    def wheel_inputs(self) -> tuple[Path, ...]:
        return self.wheels


@dataclass(frozen=True)
class RuntimeResolutionProbe(TargetProbe, RuntimeProbe[RuntimeResolution]):
    core: str
    wheels: tuple[Path, ...] = ()

    def run(self) -> RuntimeResolution:
        return RuntimeResolution(
            core_commit=self.core,
            packages=WheelPackagesProbe(self.core, self.wheels).run(),
            interpreter=InterpreterBuildProbe().run(),
        )
