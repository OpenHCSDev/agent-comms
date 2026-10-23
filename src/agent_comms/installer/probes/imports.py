from __future__ import annotations

from importlib import import_module

from agent_comms.installer.interpreter_operation import RuntimeProbe


class ClientImportsProbe(RuntimeProbe[None]):
    def run(self) -> None:
        import_module("toad.app")
        import_module("agent_comms.acp")
