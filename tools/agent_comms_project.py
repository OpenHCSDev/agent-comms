"""agent-comms as the refactor-audit's measured project: its facts, each derived from the
declaration that owns it here, and the one measure only it has, the type check of its stack
clients.

The debt ratchet's entry (debt_ratchet.py) measures an AgentCommsProject of its checkout; the
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
from collections import Counter
from collections.abc import Callable
from concurrent.futures import Executor
from dataclasses import dataclass
from functools import cache, partial
from pathlib import Path
from subprocess import CompletedProcess

import agent_comms
import stored_formats
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
from agent_comms.phase_evidence import MainTree, PlanReading
from agent_comms.pinned_audit import audit
from agent_comms.plan_phases import PlanPhase
from agent_comms.repo import Repo
from module_layers import PACKAGE, StackClient

SOURCES = Path(inspect.getfile(agent_comms)).parents[1]
REPOSITORY = SOURCES.parent


@dataclass(frozen=True)
class AgentCommsProject(audit.project.Project[PlanPhase]):
    """agent-comms' own facts, read from the modules that declare them."""

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
        return Path.home() / ".cache/agent-comms/debt-ratchet"

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

    def absence_decoders(self) -> tuple[Callable[..., object], ...]:
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
        reader = Path(inspect.getfile(stored_formats)).relative_to(REPOSITORY)
        with tempfile.TemporaryDirectory(prefix="ratchet-formats-") as scratch:
            # Each side runs its own revision's probe script: the head's may import modules
            # that only the head's package declares (#3576 moved field_codec under nominal).
            tree = Path(scratch)
            self.extract(ref, tree, str(package), str(reader))
            sources = audit.ratchet.Compared.source_paths(root, tree)
            for stamp in tree.rglob("package_stamp.py"):
                WheelStamp.of_source(tree / package, self.revision(ref),
                                     source_paths=sources).write(stamp)
            paths = dict.fromkeys((str(tree / package), *sources))
            result = SpawnedProcess.run(
                (sys.executable, "-c", self.probe, str(tree / reader),
                 os.pathsep.join(paths), *modules), cwd=scratch,
                env=audit.ratchet.Compared.environment(root, tree), timeout=300,
            )
        if result.returncode:
            raise ValueError(f"stored formats could not be read at {ref}: {result.stderr.decode()}")
        return {
            name: audit.project.StoredFormat(**form)
            for name, form in json.loads(result.stdout).items()
        }

    def carried(self, base: str, head: str) -> bool:
        """A carry step is a module under STEPS declaring a member of the Carry family: one of
        its classes derives from Carry or one of Carry's own subclasses, as that module binds it."""
        added = [path for change in self.changes(base, head, str(STEPS)) for path in change.added()
                 if self.python(path)]
        families = self.carry_family()
        for path, source in self.sources(head, set(added)):
            tree = ast.parse(source, path)
            bindings = [
                audit.source_measures.DecoratorBinding.of(family, tree, path) for family in families
            ]
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

    @cache
    def reading(self, commit: str) -> PlanReading:
        return PlanReading((), MainTree(Repo.of(self.path), commit))

    def failing_guards(self, commit: str) -> frozenset[type[PlanPhase]]:
        reading = self.reading(commit)
        return frozenset(phase for phase in PlanPhase.members_with(PlanPhase)
                         if not phase.done(reading))

    def guard_sites(self, head: str, guard: type[PlanPhase]) -> tuple[str, ...]:
        reading = self.reading(head)
        evidence = guard.phase_evidence()
        return tuple(evidence.sites(reading)) or (reading.describe(evidence),)


@dataclass(frozen=True)
class ClientFileCount(audit.ratchet.FileCount):
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
        into = Path(tempfile.mkdtemp(prefix="ratchet-clients-"))
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
        side: type[audit.ratchet.Side],
    ) -> tuple[type[audit.ratchet.Side], tuple[audit.checkers.CheckerSite, ...]]:
        # The pinned audit's git boundary, which pinned_audit puts on the path.
        commit = audit.repository.Repository(client.checkout).revision("HEAD")
        return side, repo.memo(partial(cls.checked, repo, ref, client, side),
                               cls.__name__, client.name, commit, repo.identity(ref, "src"))

    @classmethod
    def checked(cls, repo: audit.project.Project, ref: str, client: StackClient,
                side: type[audit.ratchet.Side]) -> tuple[audit.checkers.CheckerSite, ...]:
        selected = cls.selected(client)
        cache = repo.beside("mypy-clients") / client.name / side.family_name
        with tempfile.TemporaryDirectory(prefix="ratchet-client-") as scratch:
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
        return tuple(dataclasses.replace(site, path=f"{client.name}/{site.path}")
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
                     for side in audit.ratchet.Side.sides())

    @classmethod
    def measured(
        cls, found: dict[type[audit.ratchet.Side], Counter[str]], changed: set[str]
    ) -> list[str]:
        """The client files with an error on either side; changed paths are the package's."""
        return sorted(found[audit.ratchet.BaseSide].keys() | found[audit.ratchet.HeadSide].keys())

    @classmethod
    def at(cls, path: str) -> ClientFileCount:
        return ClientFileCount(cls, path)
