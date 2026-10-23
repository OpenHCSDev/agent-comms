from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from importlib.metadata import distributions
from pathlib import Path

import agent_comms
from agent_comms.installer.interpreter_operation import RuntimeProbe
from agent_comms.installer.package_records import PackageRecord


class DistributionVersionsProbe(RuntimeProbe[dict[str, str]]):
    def run(self) -> dict[str, str]:
        from packaging.utils import canonicalize_name

        return {canonicalize_name(dist.metadata["Name"]): dist.version for dist in distributions()}


@dataclass(frozen=True)
class InstalledPackagesProbe(RuntimeProbe[dict[str, PackageRecord]]):
    core: str

    def run(self) -> dict[str, PackageRecord]:
        from packaging.utils import canonicalize_name

        from agent_comms.package_stamp import STAMP

        if not Path(agent_comms.__file__).resolve().is_relative_to(Path(sys.prefix).resolve()):
            raise ValueError("Core is editable, not a built runtime package")
        actual = STAMP.commit()
        if actual != self.core:
            raise ValueError(
                "The wheel stamp does not name the selected source: "
                f"installed {actual}, requested {self.core}"
            )
        return {
            canonicalize_name(dist.name): PackageRecord.from_wire(
                {
                    "version": dist.version,
                    "source": (
                        json.loads(source)
                        if (source := dist.read_text("direct_url.json"))
                        else None
                    ),
                }
            )
            for dist in distributions()
        }
