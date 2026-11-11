"""The selected interpreter's observed native build, not its repository ancestry."""

from __future__ import annotations

import ctypes
import sys
import sysconfig
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from agent_comms.execution.child_process import Platform
from agent_comms.installer.build_artifact import BuildArtifact
from agent_comms.installer.interpreter_operation import RuntimeProbe, TargetProbe


class InterpreterBuild(BaseModel):
    """An existing build record without an interpreter observation is unqualified."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    def __str__(self) -> str:
        return "unrecorded interpreter"

    def matches(self, actual: RecordedInterpreterBuild) -> bool:
        """Absent evidence cannot make a cached runtime eligible for reuse."""
        return False

    def require_recorded(self) -> RecordedInterpreterBuild:
        raise ValueError("The runtime has no recorded interpreter build")


class RecordedInterpreterBuild(InterpreterBuild):
    """Native artifact identity, with the invoked environment kept separately."""

    invoked: Path
    version: str = Field(min_length=1)
    abi: str = Field(min_length=1)
    stdlib: Path
    artifacts: tuple[BuildArtifact, ...] = Field(min_length=1)

    def __str__(self) -> str:
        images = ", ".join(f"{artifact.path} sha256={artifact.sha256}"
                           for artifact in self.artifacts)
        return f"interpreter {self.invoked} [{self.abi}]: {images}"

    def matches(self, actual: RecordedInterpreterBuild) -> bool:
        return self == actual

    def require_recorded(self) -> RecordedInterpreterBuild:
        return self


class InterpreterBuildProbe(TargetProbe, RuntimeProbe[RecordedInterpreterBuild]):
    def run(self) -> RecordedInterpreterBuild:
        abi = sysconfig.get_config_var("SOABI")
        if not isinstance(abi, str) or not abi:
            raise ValueError("The selected interpreter does not declare its native ABI")
        address = ctypes.cast(ctypes.pythonapi.Py_GetVersion, ctypes.c_void_p)
        with Platform.current().native_image(address) as stream:
            native = BuildArtifact.from_stream(stream)
        executable = Path(sys.executable).resolve(strict=True)
        images = {native.path: native}
        if executable not in images:
            images[executable] = BuildArtifact.capture(executable)
        return RecordedInterpreterBuild(
            invoked=Path(sys.executable), version=sys.version, abi=abi,
            stdlib=Path(sysconfig.get_path("stdlib")).resolve(strict=True),
            artifacts=tuple(images[path] for path in dict.fromkeys((executable, native.path))),
        )
