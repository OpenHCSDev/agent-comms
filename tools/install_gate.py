#!/usr/bin/env python3
"""Local two-machine install check; private bus data never goes to GitHub.

    python tools/install_gate.py --pr 123
    python tools/install_gate.py --revision origin/main --base origin/main^

The PR command posts install-rehearsal on its exact head. --require-status
configures that check as required, preserving existing branch protection.
"""

from __future__ import annotations

import argparse
import ast
import asyncio
import json
import os
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import tempfile
import tomllib
from abc import abstractmethod
from collections.abc import Iterator, Mapping
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field, replace
from itertools import islice
from pathlib import Path
from typing import Annotated, ClassVar, Literal, Self

from cutover.backup import StoreBackup, UpgradeRefused
from cutover.carry import STEPS, CarryChain, CarryLedger
from install_snapshot import SnapshotHome

from agent_comms.active_route import ActiveRoute, CommsRoute
from agent_comms.check_root import CheckRoot
from agent_comms.child_process import Platform
from agent_comms.comms import Comms
from agent_comms.declared_family import DeclaredFamily
from agent_comms.field_codec import (
    FieldCodec,
    MalformedRecord,
    NullMember,
    PathText,
    RecordMember,
    SingleFieldValue,
    SubclassUnion,
)
from agent_comms.launch_runtime import LaunchRuntime, RestartEnvironment, ThreadLaunch
from agent_comms.mro_dispatch import MroDispatch, handles
from agent_comms.storage_rules import StorageRule

REPO = Path(__file__).resolve().parents[1]
CONTEXT = "install-rehearsal"
SNAPSHOT = Path(__file__).with_name("install_snapshot.py")
INSTALLER = Path("tools/upgrade_bus.py")
DEFAULT_SCRATCH = Path.home() / ".cache/agent-scratch/pi-install-gate"
DiskPath = Annotated[Path, PathText]


def run(*command: str | Path | int, cwd: Path = REPO, env: Mapping[str, str] = os.environ) -> str:
    return subprocess.run(
        tuple(map(str, command)), cwd=cwd, env=env, check=True, stdout=subprocess.PIPE, text=True
    ).stdout.strip()


class GateError(Exception):
    """A failed install check, with its private diagnostic path."""


class ExternalRecord(RecordMember):
    """Project external answers onto declared fields; FieldCodec does all decoding.

    GitHub and TOML contain unrelated fields. Ignore those at this boundary,
    not through raw mappings passed to consumers. Nested records do the same.
    """

    @classmethod
    def from_wire(cls, data: object) -> Self:
        if not isinstance(data, dict):
            raise MalformedRecord(f"Expected an object for {cls.__name__}")
        keys = {key for _, key in FieldCodec.record_fields(cls)}
        return FieldCodec.decode_record(
            cls, {key: value for key, value in data.items() if key in keys}
        )


@dataclass(frozen=True)
class WheelFiles(ExternalRecord):
    packages: tuple[str, ...]
    force_include: dict[str, str] = field(
        default_factory=dict, metadata={"wire_name": "force-include"}
    )


@dataclass(frozen=True)
class BuildHook(ExternalRecord):
    path: str


@dataclass(frozen=True)
class BuildTargets(ExternalRecord):
    wheel: WheelFiles


@dataclass(frozen=True)
class HatchBuild(ExternalRecord):
    targets: BuildTargets
    hooks: dict[str, BuildHook] = field(default_factory=dict)


@dataclass(frozen=True)
class HatchTool(ExternalRecord):
    build: HatchBuild


@dataclass(frozen=True)
class ProjectTools(ExternalRecord):
    hatch: HatchTool


@dataclass(frozen=True)
class ProjectDocument(ExternalRecord):
    tool: ProjectTools


class SourceReferences(MroDispatch):
    """Python AST declarations own their handlers, not a central type switch."""

    def __init__(self, path: str, roots: set[Path]) -> None:
        self.path = Path(path)
        self.roots = roots
        self.paths: set[str] = set()
        self.imports: set[Path] = set()

    @handles(ast.Constant)
    def constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, str):
            self.paths.add(node.value)

    @handles(ast.ImportFrom)
    def from_import(self, node: ast.ImportFrom) -> None:
        parent = self.path.parent
        if node.level:
            for _ in range(node.level - 1):
                parent = parent.parent
            self.module(parent / (node.module or "").replace(".", "/"), node.names)
        else:
            for root in (parent, Path(), *self.roots):
                self.module(root / (node.module or "").replace(".", "/"), node.names)

    def module(self, stem: Path, names: list[ast.alias]) -> None:
        self.imports.update((stem.with_suffix(".py"), stem / "__init__.py"))
        self.imports.update(stem / (alias.name + ".py") for alias in names)

    @handles(ast.Import)
    def direct_import(self, node: ast.Import) -> None:
        for alias in node.names:
            for root in (self.path.parent, Path(), *self.roots):
                self.module(root / alias.name.replace(".", "/"), [])


@dataclass(frozen=True)
class InstallSurface:
    """The distribution and installer's declared source/file dependencies."""

    repo: Path
    revision: str

    def paths(self) -> set[str]:
        tracked = set(
            filter(
                None,
                run(
                    "git", "ls-tree", "-r", "-z", "--name-only", self.revision, cwd=self.repo
                ).split("\0"),
            )
        )
        selected: set[str] = set()
        members: dict[str, set[str]] = {}
        for path in tracked:
            for prefix in (Path(path), *Path(path).parents):
                members.setdefault(str(prefix), set()).add(path)

        def include(value: str) -> None:
            value = value.rstrip("/")
            if value not in (".", ".."):
                selected.update(members.get(value, ()))

        def read(path: str) -> str:
            return run("git", "show", f"{self.revision}:{path}", cwd=self.repo)

        include("pyproject.toml")
        include(".gitmodules")
        project = FieldCodec.decode(ProjectDocument, tomllib.loads(read("pyproject.toml")))
        build = project.tool.hatch.build
        wheel = build.targets.wheel
        roots = {Path(path).parent for path in wheel.packages}
        for path in (*wheel.packages, *wheel.force_include):
            include(path)
        include(str(Path(__file__).relative_to(REPO)))
        include(str(SNAPSHOT.relative_to(REPO)))
        include(str(STEPS))
        pending = [str(INSTALLER), *(hook.path for hook in build.hooks.values())]
        seen: set[str] = set()
        while pending:
            path = pending.pop()
            if path in seen:
                continue
            seen.add(path)
            if path not in tracked:
                raise GateError(f"Missing install dependency at {self.revision}: {path}")
            include(path)
            references = SourceReferences(path, roots)
            for node in ast.walk(ast.parse(read(path), filename=path)):
                references.dispatch_sync(node)
            for referenced in references.paths:
                include(referenced)
            pending.extend(
                str(candidate) for candidate in references.imports if str(candidate) in tracked
            )
        return selected


def affected(repo: Path, base: str, head: str) -> list[str]:
    changed = set(
        filter(
            None,
            run("git", "diff", "--name-only", "-z", "--no-renames", base, head, cwd=repo).split(
                "\0"
            ),
        )
    )
    return sorted(
        changed & (InstallSurface(repo, base).paths() | InstallSurface(repo, head).paths())
    )


@dataclass(frozen=True)
class PracticeCopy:
    """A real retained tail, native session tails and the production indexes they build."""

    source: Comms
    into: Path
    max_bytes: ClassVar[int] = 3 * 1024**3

    @contextmanager
    def open(self, retained: tuple[Path, ...] = ()) -> Iterator[Comms]:
        roster = self.source.registry.snapshot()
        with self.source.bus.log.opened_wire_snapshot() as source:
            names = tuple(
                dict.fromkeys(
                    sender
                    for message in islice(
                        source.newest_messages(CheckRoot.max_bytes), CheckRoot.max_records
                    )
                    if (sender := roster.canonical_name(message.sender)) in roster.threads
                )
            )
        if not names:
            raise GateError("The retained bus tail has no registered participants")
        self.into.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if shutil.disk_usage(self.into.parent).free < storage_floor():
            raise GateError("Insufficient free space before building the retained-tail copy")
        with CheckRoot(self.source, names, self.into).open() as copied:
            required = sum(path.stat().st_size for path in retained)
            if (required > self.max_bytes
                    or shutil.disk_usage(copied.root).free < 2 * required + storage_floor()):
                raise GateError(f"Insufficient space for {required} bytes of retained stores")
            for path in retained:
                destination = copied.root / path.relative_to(self.source.root)
                destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                StoreBackup.copy_file(path, destination)
            size = sum(path.stat().st_size for path in copied.root.rglob("*") if path.is_file())
            if size > self.max_bytes:
                raise GateError(
                    f"Retained-tail copy is {size} bytes, over its {self.max_bytes} cap"
                )
            if shutil.disk_usage(self.into.parent).free < 2 * size + storage_floor():
                raise GateError(f"Insufficient free space for rehearsal of {size} copied bytes")
            yield copied

    def rehearse(
        self, target: Path, previous: Path, head: str, python: Path, log: Path, work: Path
    ) -> CompletedRehearsal:
        """The practice command and desktop gate use the same private-root lifetime."""
        ledger = CarryLedger(self.source.root)
        pending = CarryChain.at(target, head).pending(ledger.applied())
        files = {path for step in pending
                 for path in step.current(target).backup_files(self.source.root)
                 if path.is_file()}
        if ledger.path.is_file():
            files.add(ledger.path)
        with self.open(tuple(sorted(files))) as copied:
            home = self.into.parent
            prepare_runtime_home(home)
            return rehearse(target, previous, head, copied.root, home, python, log, work)


class SnapshotCopy(SnapshotHome):
    def place(self, home: Path, bus: Path) -> Path:
        original, source_home = Path(self.root), Path(self.home)
        if not original.is_relative_to(source_home):
            return bus
        relative = original.relative_to(source_home)
        if relative == Path(".") or ".." in relative.parts:
            raise GateError("Bus must be a safe subdirectory of the source home")
        selected = home / relative
        size = sum(path.stat().st_size for path in bus.rglob("*") if path.is_file())
        if shutil.disk_usage(home).free < 2 * size + storage_floor():
            raise GateError("Insufficient room to preserve the bus's home-relative position")
        selected.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        shutil.move(bus, selected)
        return selected

    def route(self, source: Path, home: Path, bus: Path) -> None:
        target = home / ".local/state/agent-comms/active-route.json"
        target.unlink(missing_ok=True)
        try:
            text = source.read_text()
        except FileNotFoundError:
            return  # No explicit selection: preserve the default home-relative bus.
        document = FieldCodec.decode(RouteDocument, FieldCodec.parse(text))
        document.require_current()
        route = document.selection
        if route.root != Path(self.root):
            raise GateError("Machine's selected route does not select its specified bus")
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        target.write_text(json.dumps(replace(route, root=bus).to_record()))
        target.chmod(0o600)


@dataclass(frozen=True)
class RouteDocument:
    version: int
    selection: ActiveRoute = field(metadata={"wire_inline": True})

    def require_current(self) -> None:
        if self.version != ActiveRoute.version:
            raise GateError(f"Snapshot route version {self.version} is not supported")


class Machine(DeclaredFamily, affix="Machine"):
    name: ClassVar[str]
    hostname: ClassVar[str]
    address: ClassVar[str]

    @property
    @abstractmethod
    def root(self) -> str: ...

    @property
    def transport(self) -> tuple[str, ...]:
        if self.hostname == socket.gethostname():
            return ()
        return ("ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", self.address)

    def command(self, *command: str) -> tuple[str, ...]:
        transport = self.transport
        return (*transport, shlex.join(command)) if transport else command

    def capture(self, destination: Path, log: Path) -> SnapshotCopy:
        destination.mkdir(mode=0o700)
        captured = 0
        floor = storage_floor()
        command: tuple[str, ...] = (
            "bwrap",
            "--ro-bind",
            "/",
            "/",
            "--unshare-all",
            "--die-with-parent",
            "/usr/bin/python3",
            "-I",
            "-B",
            "-",
            self.root,
        )
        command = self.command(*command)
        # The producer has no writable mounts, including SQLite SHM and temp.
        with ExitStack() as resources:
            errors = resources.enter_context(log.open("wb"))
            source = resources.enter_context(SNAPSHOT.open("rb"))
            reader, writer = os.pipe()
            stream = resources.enter_context(os.fdopen(reader, "rb"))
            output = resources.enter_context(os.fdopen(writer, "wb"))
            launch = resources.enter_context(
                Platform.current().launch(command, (), LaunchRuntime.running())
            )
            process = resources.enter_context(
                launch.spawn(stdin=source, stdout=output, stderr=errors)
            )
            launch.release(process.identity)
            launch.verify()
            output.close()
            try:
                with tarfile.open(fileobj=stream, mode="r|*") as archive:
                    for entry in archive:
                        if not (entry.isfile() or entry.isdir()):
                            raise GateError("Snapshot contains a link or special file")
                        if shutil.disk_usage(destination).free < captured + 2 * entry.size + floor:
                            raise GateError(
                                f"Insufficient room for snapshot and rehearsal at {destination}"
                            )
                        archive.extract(entry, destination, filter="data")
                        captured += entry.size
            except tarfile.TarError as error:
                raise GateError(f"{self.name} snapshot stream failed; see {log}") from error
            outcome = asyncio.run(process.wait())
            if not outcome.successful:
                raise GateError(f"{self.name} snapshot {outcome.summary}; see {log}")
        context = FieldCodec.decode(
            SnapshotCopy, FieldCodec.parse((destination / "context.json").read_text())
        )
        if not (destination / "bus").is_dir():
            raise GateError(f"{self.name} snapshot has no bus")
        return context

    def rehearse(
        self,
        args: GateArguments,
        target: Target,
        previous: Path,
        tree: Path,
        log: Path,
        work: Path,
    ) -> CompletedRehearsal:
        with tempfile.TemporaryDirectory(dir=args.scratch, prefix=self.name + "-") as copy:
            root = Path(copy)
            context = self.capture(root / "snapshot", log)
            home = root / "home"
            bus = prepare_home(home, root / "snapshot/bus", context)
            return rehearse(tree, previous, target.head, bus, home, args.python, log, work)


class Laptop(Machine):
    name = "laptop"
    hostname = "ts"
    address = "ts@192.168.2.18"

    @property
    def root(self) -> str:
        document = FieldCodec.decode(
            RouteDocument,
            FieldCodec.parse(
                run(
                    *self.command(
                        "sh", "-c", 'cat "$HOME/.local/state/agent-comms/active-route.json"'
                    )
                )
            ),
        )
        document.require_current()
        return str(document.selection.root)


class Desktop(Machine):
    name = "desktop"
    hostname = "desktop"
    address = "ts@192.168.2.11"
    root = "/var/tmp/agent-comms-live-20260927-wzjtqhza"

    def rehearse(
        self,
        args: GateArguments,
        target: Target,
        previous: Path,
        tree: Path,
        log: Path,
        work: Path,
    ) -> CompletedRehearsal:
        if self.transport:
            raise GateError("Run the desktop practice route on the desktop host")
        with tempfile.TemporaryDirectory(dir=args.scratch, prefix=self.name + "-") as copy:
            home = Path(copy) / "home"
            return PracticeCopy(Comms(CommsRoute.current().root), home / ".agent-comms").rehearse(
                tree, previous, target.head, args.python, log, work
            )


def storage_floor() -> float:
    return float(StorageRule.load(Path.home() / ".agent-comms").floor_gib) * 2**30


def prepare_home(home: Path, bus: Path, context: SnapshotCopy) -> Path:
    home.mkdir(mode=0o700, exist_ok=True)
    source_route = bus.parent / "active-route.json"
    bus = context.place(home, bus)
    context.route(source_route, home, bus)
    prepare_runtime_home(home)
    return bus


def prepare_runtime_home(home: Path) -> None:
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    pointer = home / ".local/share/agent-comms/current"
    pointer.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    pointer.unlink(missing_ok=True)
    pointer.symlink_to((Path.home() / ".local/share/agent-comms/current").resolve(strict=True))
    uv = home / ".local/state/agent-comms/upgrade-bus/uv"
    uv.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    uv.unlink(missing_ok=True)
    uv.symlink_to(Path.home() / ".local/state/agent-comms/upgrade-bus/uv")
    cache = Path(run("npm", "config", "get", "cache"))
    if not (home / ".npm").exists() and cache.is_dir():
        shutil.copytree(cache, home / ".npm", symlinks=True)


@contextmanager
def checkout(repo: Path, revision: str, directory: Path) -> Iterator[Path]:
    run("git", "worktree", "add", "--detach", directory, revision, cwd=repo)
    try:
        yield directory
    finally:
        run("git", "worktree", "remove", "--force", directory, cwd=repo)


@dataclass(frozen=True)
class RehearsalFiles:
    identical: int
    rewritten: int


@dataclass(frozen=True)
class RehearsalReads:
    threads: int
    who: int
    history: int


@dataclass(frozen=True)
class CompletedRehearsal(ExternalRecord):
    files: RehearsalFiles
    read: RehearsalReads
    steps: tuple[object, ...]


@dataclass(frozen=True)
class InstallerAnswer(ExternalRecord):
    rehearsal: CompletedRehearsal


def rehearse(
    target: Path,
    previous: Path,
    head: str,
    bus: Path,
    home: Path,
    python: Path,
    log: Path,
    work: Path,
) -> CompletedRehearsal:
    environment = ThreadLaunch.strip(dict(os.environ))
    RestartEnvironment.strip(environment, "root", "virtual_env", "pythonpath")
    environment.pop("AGENT_COMMS_PROJECT_SOCKET", None)
    environment.pop("AGENT_COMMS_PROJECT_REQUEST", None)
    environment.pop("PI_SESSION_ID", None)
    environment.pop("PI_SESSION_FILE", None)
    uv = Path.home() / ".local/state/agent-comms/upgrade-bus/uv"
    uv_cache = Path(run(uv, "cache", "dir"))
    uv_cache.mkdir(mode=0o700, parents=True, exist_ok=True)
    environment.update(
        HOME=str(home),
        TMPDIR=str(work),
        PYTHONDONTWRITEBYTECODE="1",
        XDG_CACHE_HOME=str(home / ".cache"),
        XDG_CONFIG_HOME=str(home / ".config"),
        XDG_STATE_HOME=str(home / ".local/state"),
        UV_CACHE_DIR=str(uv_cache),
        npm_config_cache=str(home / ".npm"),
    )
    common = Path(run("git", "rev-parse", "--path-format=absolute", "--git-common-dir", cwd=target))
    sandbox = ["bwrap", "--ro-bind", "/", "/", "--dev", "/dev", "--die-with-parent"]
    for directory in dict.fromkeys((work, home, bus.parent, common, uv_cache)):
        sandbox.extend(("--bind", str(directory), str(directory)))
    help_text = run(*sandbox, python, target / INSTALLER, "--help", cwd=target, env=environment)
    repo_option = "--repo" if "--repo " in help_text else "--checkout"
    command = (
        *sandbox,
        str(python),
        "-X",
        "faulthandler",
        str(target / INSTALLER),
        repo_option,
        str(previous),
        "--root",
        str(bus),
        "--to",
        head,
        "--installer",
        "pi-install-gate",
        "--rehearse",
        *(("--spill-dir", str(work)) if "--spill-dir " in help_text else ()),
    )
    answer = work / "installer-answer.json"
    with (
        log.open("wb") as errors,
        answer.open("wb") as output,
        Platform.current().launch(command, (), LaunchRuntime.running()) as launch,
        launch.spawn(cwd=target, env=environment, stdout=output, stderr=errors) as process,
    ):
        launch.release(process.identity)
        launch.verify()
        outcome = asyncio.run(process.wait())
    if not outcome.successful:
        reason = UpgradeRefused.public_message(log.read_text(errors="replace"))
        raise GateError(f"Rehearsal {outcome.summary}: {reason}")
    try:
        return FieldCodec.decode(InstallerAnswer, FieldCodec.parse(answer.read_text())).rehearsal
    except MalformedRecord as error:
        raise GateError(f"Installer did not return a completed rehearsal; see {log}") from error


class CommitStatus(DeclaredFamily, affix="CommitStatus"):
    @classmethod
    def post(cls, repository: str, head: str, description: str) -> None:
        run(
            "gh",
            "api",
            "--method",
            "POST",
            f"repos/{repository}/statuses/{head}",
            "-f",
            f"state={cls.declared_name}",
            "-f",
            f"context={CONTEXT}",
            "-f",
            f"description={description}",
        )


class PendingCommitStatus(CommitStatus):
    pass


class SuccessCommitStatus(CommitStatus):
    pass


class FailureCommitStatus(CommitStatus):
    pass


def api_payload(method: str, endpoint: str, payload: object) -> None:
    DEFAULT_SCRATCH.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=DEFAULT_SCRATCH, prefix="api-") as request:
        json.dump(FieldCodec.encode(payload), request)
        request.flush()
        run("gh", "api", "--method", method, endpoint, "--input", request.name)


class AppIdentity(SubclassUnion, affix="AppIdentity"):
    """GitHub's nullable application constraint, retained on protection updates."""


@dataclass(frozen=True)
class NoAppIdentity(NullMember, AppIdentity):
    pass


@dataclass(frozen=True)
class NumberedAppIdentity(SingleFieldValue, AppIdentity):
    value: int


@dataclass(frozen=True)
class RequiredCheck(ExternalRecord):
    context: str
    app_id: AppIdentity = field(default_factory=NoAppIdentity)


class StatusChecks(SubclassUnion, affix="StatusChecks"):
    @abstractmethod
    def require(self, repository: str) -> None: ...


@dataclass(frozen=True)
class NoStatusChecks(NullMember, StatusChecks):
    def require(self, repository: str) -> None:
        ExistingStatusChecks(True, ()).require(repository)


@dataclass(frozen=True)
class ExistingStatusChecks(ExternalRecord, StatusChecks):
    strict: bool
    checks: tuple[RequiredCheck, ...]

    def require(self, repository: str) -> None:
        if CONTEXT not in (check.context for check in self.checks):
            api_payload(
                "PATCH",
                f"repos/{repository}/branches/main/protection/required_status_checks",
                replace(self, checks=(*self.checks, RequiredCheck(CONTEXT))),
            )


@dataclass(frozen=True)
class ProtectionAnswer(ExternalRecord):
    required_status_checks: StatusChecks


@dataclass(frozen=True)
class UnrestrictedPolicy(NullMember):
    """The GitHub API's explicit null when adding the first protection rule."""


@dataclass(frozen=True)
class NewProtection:
    required_status_checks: ExistingStatusChecks
    enforce_admins: bool = True
    required_pull_request_reviews: UnrestrictedPolicy = field(default_factory=UnrestrictedPolicy)
    restrictions: UnrestrictedPolicy = field(default_factory=UnrestrictedPolicy)


@dataclass(frozen=True)
class MissingProtection(ExternalRecord):
    """Only GitHub's documented not-found answer permits creating protection."""

    status: Literal["404"]


def require_status(repository: str) -> None:
    response = subprocess.run(
        ("gh", "api", f"repos/{repository}/branches/main/protection"),
        capture_output=True,
        text=True,
    )
    if response.returncode:
        FieldCodec.decode(MissingProtection, FieldCodec.parse(response.stdout))
        api_payload(
            "PUT",
            f"repos/{repository}/branches/main/protection",
            NewProtection(ExistingStatusChecks(True, (RequiredCheck(CONTEXT),))),
        )
    else:
        FieldCodec.decode(
            ProtectionAnswer, FieldCodec.parse(response.stdout)
        ).required_status_checks.require(repository)


class MachineResult(DeclaredFamily, affix="MachineResult"):
    @property
    @abstractmethod
    def successful(self) -> bool: ...

    @property
    def failures(self) -> tuple[str, ...]:
        return ()


@dataclass(frozen=True)
class PassedMachineResult(MachineResult):
    rehearsal: CompletedRehearsal

    @property
    def successful(self) -> bool:
        return True


@dataclass(frozen=True)
class UnfinishedMachineResult(MachineResult):
    @property
    def successful(self) -> bool:
        return False


@dataclass(frozen=True)
class FailedMachineResult(MachineResult):
    error: str

    @property
    def successful(self) -> bool:
        return False

    @property
    def failures(self) -> tuple[str, ...]:
        return (self.error,)


class GateResult(DeclaredFamily, affix="GateResult"):
    @property
    def failures(self) -> tuple[str, ...]:
        return (self.description,)

    @property
    @abstractmethod
    def successful(self) -> bool: ...

    @property
    @abstractmethod
    def description(self) -> str: ...

    def publish(self, repository: str, head: str) -> None:
        status = SuccessCommitStatus if self.successful else FailureCommitStatus
        status.post(repository, head, self.description)


@dataclass(frozen=True)
class OutsideInstallGateResult(GateResult):
    @property
    def successful(self) -> bool:
        return True

    @property
    def description(self) -> str:
        return "Outside install/runtime distribution"


@dataclass(frozen=True)
class MachinesGateResult(GateResult):
    machines: dict[str, MachineResult]

    @property
    def failures(self) -> tuple[str, ...]:
        return tuple(
            f"{name}: {reason}"
            for name, result in self.machines.items()
            for reason in result.failures
        )

    @property
    def successful(self) -> bool:
        return len(self.machines) == 2 and all(
            result.successful for result in self.machines.values()
        )

    @property
    def description(self) -> str:
        return (
            "Both rehearsals passed"
            if self.successful
            else "Install rehearsal failed; local report retained"
        )


@dataclass(frozen=True)
class PracticeGateResult(GateResult):
    rehearsal: CompletedRehearsal

    @property
    def successful(self) -> bool:
        return True

    @property
    def description(self) -> str:
        return "Retained-tail practice install passed"


@dataclass(frozen=True)
class UnfinishedGateResult(MachinesGateResult):
    @property
    def successful(self) -> bool:
        return False


@dataclass(frozen=True)
class InterruptedGateResult(MachinesGateResult):
    error: str

    @property
    def failures(self) -> tuple[str, ...]:
        return (self.error, *super().failures)

    @property
    def successful(self) -> bool:
        return False


class SourceCoverage(DeclaredFamily, affix="SourceCoverage"):
    """An interrupted source read is not an empty set of affected paths."""


@dataclass(frozen=True)
class UncheckedSourceCoverage(SourceCoverage):
    pass


@dataclass(frozen=True)
class CheckedSourceCoverage(SourceCoverage):
    paths: tuple[str, ...]


@dataclass(frozen=True)
class GateReport:
    head: str
    base: str
    affected: SourceCoverage
    result: GateResult


@dataclass(frozen=True)
class RepositoryAnswer(ExternalRecord):
    name: str = field(metadata={"wire_name": "nameWithOwner"})


@dataclass(frozen=True)
class PullRequestAnswer(ExternalRecord):
    head: str = field(metadata={"wire_name": "headRefOid"})
    base: str = field(metadata={"wire_name": "baseRefOid"})
    cross_repository: bool = field(metadata={"wire_name": "isCrossRepository"})

    def require_local(self) -> None:
        if self.cross_repository:
            raise GateError("Execute private-data rehearsals only for reviewed same-repository PRs")


@dataclass(frozen=True)
class Target:
    head: str
    base: str

    def start(self, repository: str, repo: Path) -> None:
        pass

    def outside(self, changed: tuple[str, ...]) -> bool:
        return False  # A revision command forces practice installs even without affected paths.

    def finish(self, repository: str, result: GateResult) -> None:
        pass


class PullRequestTarget(Target):
    def start(self, repository: str, repo: Path) -> None:
        PendingCommitStatus.post(
            repository, self.head, "Checking source ownership and both machines"
        )
        run("git", "fetch", "-q", "origin", self.base, self.head, cwd=repo)

    def outside(self, changed: tuple[str, ...]) -> bool:
        return not changed

    def finish(self, repository: str, result: GateResult) -> None:
        result.publish(repository, self.head)


class BaseRevision(DeclaredFamily, affix="BaseRevision"):
    @abstractmethod
    def resolve(self, repo: Path, head: str) -> str: ...


@dataclass(frozen=True)
class ParentBaseRevision(BaseRevision):
    def resolve(self, repo: Path, head: str) -> str:
        return run("git", "rev-parse", f"{head}^", cwd=repo)


@dataclass(frozen=True)
class NamedBaseRevision(BaseRevision):
    revision: str

    def resolve(self, repo: Path, head: str) -> str:
        return run("git", "rev-parse", f"{self.revision}^{{commit}}", cwd=repo)


class GateCommand(DeclaredFamily, affix="GateCommand"):
    @abstractmethod
    def execute(self, args: GateArguments, repository: str) -> int: ...


class CheckGateCommand(GateCommand):
    def run(
        self, args: GateArguments, target: Target, report: Path, results: dict[str, MachineResult]
    ) -> GateResult:
        return MachinesGateResult(run_machines(args, target, report, results))

    @abstractmethod
    def target(self, args: GateArguments, repository: str) -> Target: ...

    def execute(self, args: GateArguments, repository: str) -> int:
        target = self.target(args, repository)
        report_path = args.scratch / f"report-{target.head[:12]}-{os.getpid()}.json"
        coverage: SourceCoverage = UncheckedSourceCoverage()
        results: dict[str, MachineResult] = {}
        result: GateResult = UnfinishedGateResult(results)
        try:
            target.start(repository, args.repo)
            changed = tuple(affected(args.repo, target.base, target.head))
            coverage = CheckedSourceCoverage(changed)
            result = (
                OutsideInstallGateResult()
                if target.outside(changed)
                else self.run(args, target, report_path, results)
            )
        except (
            GateError,
            subprocess.CalledProcessError,
            MalformedRecord,
            KeyboardInterrupt,
        ) as error:
            result = InterruptedGateResult(results, str(error))
        finally:
            report_path.write_text(
                json.dumps(
                    FieldCodec.encode(GateReport(target.head, target.base, coverage, result)),
                    indent=2,
                )
                + "\n"
            )
            target.finish(repository, result)
            print(json.dumps(FieldCodec.encode(RunSummary(result, report_path)), indent=2))
        return 0 if result.successful else 1


@dataclass(frozen=True)
class RunSummary:
    result: GateResult
    report: DiskPath


@dataclass(frozen=True)
class PrGateCommand(CheckGateCommand):
    number: int

    @classmethod
    def argument(cls, text: str) -> PrGateCommand:
        return cls(int(text))

    def target(self, args: GateArguments, repository: str) -> Target:
        answer = FieldCodec.decode(
            PullRequestAnswer,
            FieldCodec.parse(
                run(
                    "gh",
                    "pr",
                    "view",
                    self.number,
                    "--repo",
                    repository,
                    "--json",
                    "headRefOid,baseRefOid,isCrossRepository",
                )
            ),
        )
        answer.require_local()
        return PullRequestTarget(answer.head, answer.base)


@dataclass(frozen=True)
class RevisionGateCommand(CheckGateCommand):
    revision: str

    def target(self, args: GateArguments, repository: str) -> Target:
        head = run("git", "rev-parse", f"{self.revision}^{{commit}}", cwd=args.repo)
        return Target(head, args.base.resolve(args.repo, head))


@dataclass(frozen=True)
class PracticeGateCommand(RevisionGateCommand):
    def run(
        self, args: GateArguments, target: Target, report: Path, results: dict[str, MachineResult]
    ) -> GateResult:
        with tempfile.TemporaryDirectory(dir=args.scratch, prefix="practice-") as directory:
            private = Path(directory)
            home = private / "home"
            with tempfile.TemporaryDirectory(
                dir=Path.home() / "wt", prefix="practice-source-"
            ) as source:
                work = Path(source)
                with (
                    checkout(args.repo, target.base, work / "base") as previous,
                    checkout(args.repo, target.head, work / "target") as tree,
                ):
                    completed = PracticeCopy(
                        Comms(CommsRoute.current().root), home / ".agent-comms"
                    ).rehearse(
                        tree, previous, target.head, args.python, report.with_suffix(".log"), work
                    )
            return PracticeGateResult(completed)


@dataclass(frozen=True)
class ConfigureGateCommand(GateCommand):
    def execute(self, args: GateArguments, repository: str) -> int:
        require_status(repository)
        return 0


class GateArguments(argparse.Namespace):
    command: GateCommand
    base: BaseRevision
    repo: Path
    scratch: Path
    python: Path


def run_machines(
    args: GateArguments, target: Target, report: Path, results: dict[str, MachineResult]
) -> dict[str, MachineResult]:
    machines = tuple(host() for host in Machine.members_with(Machine))
    results.update((machine.name, UnfinishedMachineResult()) for machine in machines)
    with tempfile.TemporaryDirectory(dir=Path.home() / "wt", prefix="install-gate-") as directory:
        work = Path(directory)
        with (
            checkout(args.repo, target.base, work / "base") as previous,
            checkout(args.repo, target.head, work / "target") as tree,
        ):
            for machine in machines:
                log = report.with_name(report.stem + f"-{machine.name}.log")
                try:
                    results[machine.name] = PassedMachineResult(
                        machine.rehearse(args, target, previous, tree, log, work)
                    )
                except (GateError, subprocess.CalledProcessError, MalformedRecord) as error:
                    results[machine.name] = FailedMachineResult(str(error))
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--pr", dest="command", type=PrGateCommand.argument)
    mode.add_argument("--revision", dest="command", type=RevisionGateCommand)
    mode.add_argument("--practice", dest="command", type=PracticeGateCommand)
    mode.add_argument(
        "--require-status", dest="command", action="store_const", const=ConfigureGateCommand()
    )
    parser.add_argument("--base", type=NamedBaseRevision, default=ParentBaseRevision())
    parser.add_argument("--repo", type=Path, default=REPO)
    parser.add_argument("--scratch", type=Path, default=DEFAULT_SCRATCH)
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    args = parser.parse_args(namespace=GateArguments())
    args.scratch.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.umask(0o077)
    repository = FieldCodec.decode(
        RepositoryAnswer,
        FieldCodec.parse(run("gh", "repo", "view", "--json", "nameWithOwner", cwd=args.repo)),
    )
    return args.command.execute(args, repository.name)


def stopped(signum: int, frame: object) -> None:
    raise KeyboardInterrupt(f"Stopped by signal {signum}")


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, stopped)
    raise SystemExit(main())
