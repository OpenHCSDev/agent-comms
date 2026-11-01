"""The selected interpreter's observed native build, not its repository ancestry."""

from __future__ import annotations

import ctypes
import os
from pathlib import Path
import sys
import sysconfig

from pydantic import BaseModel, ConfigDict, Field

from agent_comms.installer.interpreter_operation import RuntimeProbe, TargetProbe
from agent_comms.build_artifact import BuildArtifact


class PythonNativeImage(ctypes.Structure):
    """POSIX dladdr's ABI, queried for the image actually providing Py_GetVersion."""

    _fields_ = (
        ("filename", ctypes.c_char_p),
        ("base", ctypes.c_void_p),
        ("symbol", ctypes.c_char_p),
        ("address", ctypes.c_void_p),
    )

    @classmethod
    def running(cls) -> Path:
        lookup = ctypes.CDLL(None).dladdr
        lookup.argtypes = (ctypes.c_void_p, ctypes.POINTER(cls))
        lookup.restype = ctypes.c_int
        image = cls()
        if not lookup(ctypes.cast(ctypes.pythonapi.Py_GetVersion, ctypes.c_void_p),
                      ctypes.byref(image)) or not image.filename:
            raise ValueError("The loader did not identify the running Python native image")
        return Path(os.fsdecode(image.filename)).resolve(strict=True)


class InterpreterBuild(BaseModel):
    """An existing build record without an interpreter observation is unqualified."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    def __str__(self) -> str:
        return "unrecorded interpreter"

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

    def require_recorded(self) -> RecordedInterpreterBuild:
        return self


class InterpreterBuildProbe(TargetProbe, RuntimeProbe[RecordedInterpreterBuild]):
    def run(self) -> RecordedInterpreterBuild:
        abi = sysconfig.get_config_var("SOABI")
        if not isinstance(abi, str) or not abi:
            raise ValueError("The selected interpreter does not declare its native ABI")
        images = dict.fromkeys((Path(sys.executable).resolve(strict=True),
                                PythonNativeImage.running()))
        return RecordedInterpreterBuild(
            invoked=Path(sys.executable), version=sys.version, abi=abi,
            stdlib=Path(sysconfig.get_path("stdlib")).resolve(strict=True),
            artifacts=tuple(BuildArtifact.capture(path) for path in images),
        )
