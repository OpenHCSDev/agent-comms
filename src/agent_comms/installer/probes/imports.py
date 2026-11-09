from __future__ import annotations

from importlib import import_module
from pkgutil import walk_packages
from typing import NoReturn

from agent_comms.installer.interpreter_operation import RuntimeProbe, TargetProbe
from agent_comms.installer.runtime_resolution import ToadPackage


class ClientImportsProbe(TargetProbe, RuntimeProbe[None]):
    """Every module of the client the runtime ships imports, not only the ones toad.app
    reaches at start: on 2026-11-06 toad.app imported on a formbridge without the `shows`
    class keyword while toad.forms.editors, opened later from Settings, could not."""

    def run(self) -> None:
        root = ToadPackage.import_root
        package = import_module(root)
        for module in walk_packages(package.__path__, f"{root}.", onerror=self.refuse):
            import_module(module.name)
        import_module("agent_comms.execution.acp")

    @staticmethod
    def refuse(name: str) -> NoReturn:
        """walk_packages ignores a package that fails to import unless this re-raises."""
        raise
