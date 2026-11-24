"""Distribution inventory probes using PEP 503 canonicalization."""

from __future__ import annotations

from importlib.metadata import Distribution, distributions

from packaging.utils import canonicalize_name

from agent_comms.installer.interpreter_operation import RuntimeProbe, TargetProbe


class DistributionProbe(TargetProbe):
    """The selected interpreter's distribution inventory, rejecting duplicate identities."""

    @staticmethod
    def distributions() -> dict[str, Distribution]:
        installed = tuple(distributions())
        selected: dict[str, Distribution] = {
            canonicalize_name(dist.name): dist for dist in installed
        }
        if len(selected) != len(installed):
            raise ValueError("The runtime contains duplicate distribution identities")
        return selected


class DistributionVersionsProbe(DistributionProbe, RuntimeProbe[dict[str, str]]):
    def run(self) -> dict[str, str]:
        return {name: dist.version for name, dist in self.distributions().items()}
