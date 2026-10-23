"""Declared operations sent to an interpreter, with answers separate from diagnostics.

Runtime probes execute this file by path with ``python -P``. The selected runtime
supplies its own imports even when it predates this file.
"""

from __future__ import annotations

import base64
import inspect
import json
import os
import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import cast
from zipfile import ZIP_DEFLATED, ZipFile

from typing_extensions import TypeForm

from agent_comms.declared_family import DeclaredFamily
from agent_comms.field_codec import FieldCodec
from agent_comms.maintenance_barrier import HOLDER_NONCE


class InterpreterOperation[Answer, *Arguments](ABC):
    """The operation's return annotation owns its answer on both sides of the process."""

    @abstractmethod
    def run(self, *arguments: *Arguments) -> Answer: ...

    def answer_type(self) -> TypeForm[Answer]:
        return cast(TypeForm[Answer], inspect.signature(self.run, eval_str=True).return_annotation)

    def encode_answer(self, answer: Answer) -> str:
        return json.dumps(FieldCodec.encode(answer, self.answer_type()))

    def decode_answer(self, answer: str) -> Answer:
        return FieldCodec.decode(self.answer_type(), FieldCodec.parse(answer))


class ProbeProgram(ABC):
    """Where an operation's declaration is executable by the selected interpreter."""

    @abstractmethod
    def entry(self) -> tuple[str | Path, ...]: ...

    def import_roots(self) -> tuple[Path, ...]:
        """Directories the interpreter imports from beside its own packages."""
        return ()


class SourceProgram(ProbeProgram):
    """The target's file can run against a predecessor that does not declare it yet."""

    def entry(self) -> tuple[str, str]:
        return RetainedProgram.current().entry()


class InstalledProgram(ProbeProgram):
    """A published runtime owns its declaration after the target tree is retired."""

    def entry(self) -> tuple[str, str]:
        return "-c", f"from {__name__} import serve; serve()"


@dataclass(frozen=True)
class TargetProgram(InstalledProgram):
    """The runtime built from the target tree declares every operation, carry passes
    included; their steps ship in no runtime, so the tree's ``steps`` supply them."""

    steps: Path

    def import_roots(self) -> tuple[Path]:
        return (self.steps,)


@dataclass(frozen=True)
class RetainedProgram(ProbeProgram):
    """A source operation that must finish after its owning checkout closes. It runs as the
    interpreter's main module, so the selected runtime's own imports keep its declarations."""

    source: str

    @classmethod
    def current(cls) -> RetainedProgram:
        """Retain the installer declarations after the target checkout is retired.

        The selected runtime supplies Core's other packages. The snapshot supplies
        this installer's operation family and its discoverable probe package.
        """
        directory = Path(inspect.getfile(RuntimeProbe)).parent
        output = BytesIO()
        with ZipFile(output, "w", compression=ZIP_DEFLATED) as archive:
            for path in sorted(directory.rglob("*.py")):
                archive.writestr(str(path.relative_to(directory.parent.parent)), path.read_bytes())
        payload = base64.b85encode(output.getvalue()).decode("ascii")
        return cls(
            "import base64\n"
            "from pathlib import Path\n"
            "from tempfile import TemporaryDirectory\n"
            "import agent_comms.installer\n"
            "cache = Path.home() / '.cache'\n"
            "cache.mkdir(parents=True, exist_ok=True)\n"
            "with TemporaryDirectory(prefix='agent-comms-probes-', dir=cache) as retained:\n"
            "    archive = Path(retained) / 'installer.zip'\n"
            f"    archive.write_bytes(base64.b85decode({payload!r}))\n"
            "    agent_comms.installer.__path__ = [str(archive) + '/agent_comms/installer']\n"
            "    from agent_comms.installer.interpreter_operation import serve\n"
            "    serve()\n"
        )

    def entry(self) -> tuple[str, str]:
        return "-c", self.source


@dataclass(frozen=True)
class RuntimeProbe[Answer](
    InterpreterOperation[Answer],
    DeclaredFamily,
    affix="Probe",
    discovery_package="agent_comms.installer.probes",
):
    """A declared operation handled by the install's selected interpreter worker."""


@dataclass(frozen=True)
class ProbeRequest:
    """One operation and the maintenance hold its caller acts under. A worker outlives holds,
    so the holder travels with each request and never with the worker's launch."""

    probe: RuntimeProbe
    holder: str

    def answer(self) -> str:
        """The worker acts as its caller's holder for this request. Its runtime may predate
        ``MaintenanceHolder``, so this sets the variable that owner reads; every request
        sets it, so no earlier request's holder remains."""
        os.environ[HOLDER_NONCE] = self.holder
        return self.probe.encode_answer(self.probe.run())


def serve() -> None:
    """Answer declared operations from stdin, one encoded answer per request line, until it
    closes. An operation that raises ends the worker: its traceback goes to stderr, and the
    caller reads the exit in place of an answer. The answers own stdout's descriptor; every
    other write, an operation's exit handlers included, lands on stderr."""
    answers = os.fdopen(os.dup(sys.stdout.fileno()), "w")
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    for line in sys.stdin:
        print(
            FieldCodec.decode(ProbeRequest, FieldCodec.parse(line)).answer(),
            file=answers,
            flush=True,
        )
