"""agent-comms as NominalRefactorAdvisor's measured project: its facts, each derived from the
declaration that owns it here, and the one measure only it has, the type check of its stack
clients.

The NRA gate's entry (nra_gate.py) measures an AgentCommsProject of its checkout; NRA's
audit package itself never imports agent-comms."""

from __future__ import annotations

import ast
import atexit
import dataclasses
import inspect
import json
import os
import shutil
import sys
import tempfile
from abc import ABC, abstractmethod
from collections import Counter
from collections.abc import Callable
from concurrent.futures import Executor
from dataclasses import dataclass
from functools import cache, partial
from pathlib import Path
from subprocess import CompletedProcess

import stored_formats
from module_layers import PACKAGE, StackClient
from nominal_refactor_advisor import audit

import agent_comms
from agent_comms import (
    coordination_schema,
    field_codec,
    locked_store,
    process_claim,
    store_files,
    typed_table,
)
from agent_comms.child_process import ForkedProcessPool, SpawnedProcess
from agent_comms.core_stamp import WheelStamp
from agent_comms.declared_family import DeclaredFamily
from agent_comms.field_codec import boundary_decoder
from agent_comms.installer.carry import STEPS
from agent_comms.installer.probes.carry import Carry
from agent_comms.nominal_census import NominalCensus
from agent_comms.phase_evidence import MainTree, PlanReading
from agent_comms.plan_phases import PlanPhase
from agent_comms.repo import Repo

SOURCES = Path(inspect.getfile(agent_comms)).parents[1]
REPOSITORY = SOURCES.parent
READER = Path(inspect.getfile(stored_formats)).relative_to(REPOSITORY)


class StoredFormatReader(ABC):
    """Where the reader that declares a revision's stored formats comes from. The measured
    project declares it once; nothing probes a revision for one."""

    @abstractmethod
    def place(self, project: AgentCommsProject, ref: str, tree: Path) -> None:
        """Write the reader to its path under the extracted revision."""


class RevisionReader(StoredFormatReader):
    """agent-comms measuring itself: each revision runs its own reader, because a head's
    reader may import modules only that head declares (#3576 moved field_codec under
    nominal). The audit it imports is the installed NominalRefactorAdvisor's. A revision
    without a reader fails loud."""

    def place(self, project: AgentCommsProject, ref: str, tree: Path) -> None:
        project.extract(ref, tree, str(READER))


class CheckoutReader(StoredFormatReader):
    """A client repository such as Toad ships no reader; the gate checkout's own reads its
    modules, importing agent_comms from that checkout."""

    def place(self, project: AgentCommsProject, ref: str, tree: Path) -> None:
        (tree / READER).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPOSITORY / READER, tree / READER)


@dataclass(frozen=True)
class AgentCommsProject(audit.project.Project[PlanPhase]):
    """agent-comms' own facts, read from the modules that declare them."""

    reader: StoredFormatReader = RevisionReader()

    probe = (
        "import importlib.util, os, sys; sys.path[:0] = sys.argv[2].split(os.pathsep); "
        "spec = importlib.util.spec_from_file_location('stored_formats', sys.argv[1]); "
        "module = sys.modules[spec.name] = importlib.util.module_from_spec(spec); "
        "spec.loader.exec_module(module); "
        "module.main(sys.argv[3:])"
    )
    """The revision's own reader, loaded by file, over only the revision's own source: a tools
    directory on the path would resolve the revision's bare imports into this one's tree."""

    def cache_directory(self) -> Path:
        return Path.home() / ".cache/agent-comms/nra-gate"

    def source_roots(self) -> tuple[Path, ...]:
        return (Path(inspect.getfile(stored_formats)).parent, SOURCES)

    def pool(self, workers: int) -> Executor:
        return ForkedProcessPool(workers)

    def run(self, command: tuple[str, ...], *, cwd: Path, env: dict[str, str],
            timeout: float) -> CompletedProcess[bytes]:
        return SpawnedProcess.run(command, cwd=cwd, env=env, timeout=timeout)

    def lock_owners(self) -> tuple[str, ...]:
        return tuple(Path(inspect.getfile(module)).relative_to(SOURCES).as_posix()
                     for module in (store_files, locked_store, process_claim))

    def lock_functions(self) -> frozenset[str]:
        return frozenset(function.__name__
                         for function in (store_files._store_lock, store_files._async_store_lock))

    def absence_boundaries(self) -> tuple[Callable[..., object], ...]:
        return (boundary_decoder,)

    def mechanism_modules(self) -> frozenset[Path]:
        # Our own modules are named by their files; Toad's live in another repository, by path.
        return frozenset((
            *(Path(inspect.getfile(module)).relative_to(REPOSITORY)
              for module in (field_codec, typed_table, coordination_schema)),
            *(Path("src", "toad", module) for module in ("agent_schema.py", "db.py")),
        ))

    def selecting_calls(self) -> frozenset[str]:
        return frozenset({DeclaredFamily.member_named.__name__})

    def stored_formats(
        self, ref: str, root: str, changed: set[str]
    ) -> dict[str, audit.project.StoredFormat]:
        package = Path(root)
        while self.tracks(ref, str(package / "__init__.py")):
            package = package.parent
        modules = [".".join(Path(path).relative_to(package).with_suffix("").parts)
                   for path in sorted(changed)]
        with tempfile.TemporaryDirectory(prefix="gate-formats-") as scratch:
            tree = Path(scratch)
            self.extract(ref, tree, str(package))
            self.reader.place(self, ref, tree)
            sources = audit.gate.Compared.source_paths(root, tree)
            for stamp in tree.rglob("package_stamp.py"):
                WheelStamp.of_source(tree / package, self.revision(ref),
                                     source_paths=sources).write(stamp)
            paths = dict.fromkeys((str(tree / package), *sources))
            result = SpawnedProcess.run(
                (sys.executable, "-c", self.probe, str(tree / READER),
                 os.pathsep.join(paths), *modules), cwd=scratch,
                env=audit.gate.Compared.environment(root, tree), timeout=300,
            )
        if result.returncode:
            raise ValueError(f"stored formats could not be read at {ref}: {result.stderr.decode()}")
        return {name: audit.project.StoredFormat.declared(**form)
                for name, form in json.loads(result.stdout).items()}

    def carried(self, base: str, head: str) -> bool:
        """A carry step is a module under STEPS declaring a member of the Carry family: one of
        its classes derives from Carry or one of Carry's own subclasses, as that module binds it."""
        added = [path for change in self.changes(base, head, str(STEPS)) for path in change.added()
                 if self.python(path)]
        families = self.carry_family()
        for path, source in self.sources(head, set(added)):
            tree = ast.parse(source, path)
            module = audit.lexical.ModuleScope(tree, path)
            bindings = [audit.lexical.DecoratorBinding.of(family, module) for family in families]
            if any(binding.marks(base) for node in ast.walk(tree) if isinstance(node, ast.ClassDef)
                   for base in node.bases for binding in bindings):
                return True
        return False

    @staticmethod
    @cache
    def carry_family() -> tuple[type, ...]:
        """Carry and every class a step derives from on its way to Carry: each member's MRO up to
        Carry, so a base another step declares, such as ToadSettingsCarry, is one of them."""
        return tuple(dict.fromkeys(
            base for member in Carry.members_with(Carry)
            for base in member.__mro__[:member.__mro__.index(Carry) + 1]
        ))

    def guard_roots(self) -> frozenset[str]:
        declared = Path(inspect.getfile(PlanPhase)).resolve()
        return frozenset({f"{declared.relative_to(REPOSITORY).parts[0]}/"})

    @staticmethod
    @cache
    def reading(path: Path, commit: str) -> PlanReading:
        """The plan phases as the commit declares them, read once per checkout and commit."""
        return PlanReading((), MainTree(Repo.of(path), commit, NominalCensus()))

    def failing_guards(self, commit: str) -> frozenset[type[PlanPhase]]:
        reading = self.reading(self.path, commit)
        return frozenset(phase for phase in PlanPhase.members_with(PlanPhase)
                         if not phase.done(reading))

    def guard_sites(self, head: str, guard: type[PlanPhase]) -> tuple[str, ...]:
        reading = self.reading(self.path, head)
        evidence = guard.phase_evidence()
        return tuple(evidence.sites(reading)) or (reading.describe(evidence),)


@dataclass(frozen=True)
class ClientFileCount(audit.gate.FileCount):
    """Type errors in one stack client file, which the client's own code cannot add: both sides
    check the same client commit."""

    def grew(self, grown: int) -> str:
        return (f"grew {self.text} +{grown}: this change removes or changes an agent_comms name "
                "that client file uses; run mypy on it to see which")


class ClientTypeErrors(audit.checkers.SideCounts):
    """Mypy errors in each stack client file that imports agent_comms, checked against each
    revision's package. Both sides read the same client commit, so the client's own errors cancel
    and an error the head adds is a name or signature the client uses that the change took away:
    #3110 deleted TurnState.settles as unused while Toad's conversation_turn.py:262 called it, and
    only the running app found it."""

    @staticmethod
    @cache
    def clients(repo: audit.project.Project, head: str) -> tuple[StackClient, ...]:
        """The head's stack clients, cloned once per run and read by both sides; a revision
        declaring no stack has none."""
        stack = "stack/pyproject.toml"
        if not repo.tracks(head, stack):
            return ()
        into = Path(tempfile.mkdtemp(prefix="gate-clients-"))
        atexit.register(shutil.rmtree, into, True)
        (into / "stack").mkdir()
        (into / stack).write_bytes(repo.blob(head, stack))
        return StackClient.fetched(into / "stack", into)

    @staticmethod
    def selected(client: StackClient) -> list[str]:
        return sorted({entry.path for entry in client.imports
                       if entry.module.partition(".")[0] == PACKAGE})

    @classmethod
    def findings(
        cls, repo: audit.project.Project, ref: str, client: StackClient,
        side: type[audit.gate.Side],
    ) -> tuple[type[audit.gate.Side], tuple[audit.checkers.CheckerSite, ...]]:
        """Mypy resolves the client's other imports from the installed environment, which no
        content key names, so each comparison checks afresh under mypy's own cache."""
        selected = cls.selected(client)
        cache = repo.beside("mypy-clients") / client.name / side.family_name
        with tempfile.TemporaryDirectory(prefix="gate-client-") as scratch:
            repo.extract(ref, Path(scratch), "src")
            # The client checks under its own configuration, from its checkout.
            result = SpawnedProcess.run(
                (sys.executable, "-m", *audit.checkers.TypeErrors.mypy(
                    cache, "--follow-imports=silent", "--ignore-missing-imports", *selected)),
                cwd=client.checkout, timeout=600,
                env={**os.environ, "MYPYPATH": os.pathsep.join(
                    (str(Path(scratch, "src")), str(client.checkout / "src")))},
            )
        output = cls.checker_output(result, f"{client.name} at {ref}")
        return side, tuple(dataclasses.replace(site, path=f"{client.name}/{site.path}")
                           for site in audit.checkers.TypeErrors.reported(output)
                           if site.path in selected)

    @classmethod
    def parts(cls, repo: audit.project.Project, base: str, head: str, changed: set[str],
              root: str) -> tuple[Callable[[], object], ...]:
        # Only a change to the package the clients import can change what they see.
        if not any(path.startswith(f"src/{PACKAGE}/") for path in changed):
            return ()
        return tuple(partial(cls.findings, repo, side.ref(base, head), client, side)
                     for client in cls.clients(repo, head)
                     if cls.selected(client)
                     for side in audit.gate.Side.sides())

    @classmethod
    def measured(
        cls, found: dict[type[audit.gate.Side], Counter[str]], changed: set[str]
    ) -> list[str]:
        """The client files with an error on either side; changed paths are the package's."""
        return sorted(found[audit.gate.BaseSide].keys() | found[audit.gate.HeadSide].keys())

    @classmethod
    def at(cls, path: str) -> ClientFileCount:
        return ClientFileCount(cls, path)
