"""Native child custody: empty, borrowed, retained, retiring and strict reopen."""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from contextlib import ExitStack, asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

from .child_process import AttachedChild
from .native_attestation import NativeAttestation, PendingAttestation, SavedSessionReopenError
from .native_pi import NativePiRpcLaunch, NativePiUnavailable
from .native_session_reopen import NativeSessionIdentity
from .pi_rpc import PiRpcChannel
from .private_path import PrivateSocketRole

if TYPE_CHECKING:
    from .compaction_records import CompactionOperation
    from .pi_vocabulary import CompactionReason
    from .selected_source import SessionRevision
    from .owner_compaction_prepare import NativeWitness


@dataclass
class PiSessionChild:
    proc: AttachedChild
    reader: PiRpcChannel
    stderr_task: asyncio.Task[str]
    key: tuple[NativePiRpcLaunch, tuple[int, int]]
    attestation: NativeAttestation
    sensitive_diagnostics: bool = False
    resources: ExitStack = field(default_factory=ExitStack, repr=False)

    async def reply_ui(self, response) -> None:
        await asyncio.wait_for(self.proc.write(self.reader.encode(response)), timeout=2)

    @classmethod
    async def start(cls, key, attestation):
        launch, _ = key
        with ExitStack() as resources:
            environment = dict(launch.env)
            if "AGENT_COMMS_PROJECT_SOCKET" in environment:
                address = resources.enter_context(PrivateSocketRole.address(
                    Path(environment["AGENT_COMMS_PROJECT_SOCKET"])
                ))
                environment["AGENT_COMMS_PROJECT_SOCKET"] = str(address)
            proc = await AttachedChild.start(launch.argv, cwd=str(launch.cwd), env=environment)
            assert proc.stdout is not None and proc.stderr is not None
            return cls(
                proc,
                PiRpcChannel(proc.stdout),
                asyncio.create_task(cls.stderr_tail(proc.stderr)),
                key,
                attestation,
                resources=resources.pop_all(),
            )

    @staticmethod
    async def stderr_tail(stream: asyncio.StreamReader) -> str:
        tail = b""
        while chunk := await stream.read(4096):
            tail = (tail + chunk)[-16000:]
        return tail.decode(errors="replace").strip()

    async def close(self) -> None:
        await self.proc.stop()
        await asyncio.gather(self.stderr_task, return_exceptions=True)
        self.resources.close()

    @asynccontextmanager
    async def failures(self):
        """Retain original bounded child output before transporting its failure."""
        try:
            yield
        except BaseException as error:
            await self.close()
            stderr = await self.stderr_task
            error.add_note(f"Original native child stderr:\n{stderr or '(empty)'}")
            if isinstance(error, NativePiUnavailable):
                error.native_stderr = stderr
            raise


class NativeCustody(ABC):
    """State owns the legal child capabilities, including its retirement successor."""

    @property
    @abstractmethod
    def available(self) -> bool: ...

    retained = False

    def managed_launch(self, command, arguments, *, worktree, environment, session_file):
        return NativePiRpcLaunch.managed(command, arguments, worktree=worktree,
            environment=environment, session_file=session_file)

    def reuse(self, key) -> PiSessionChild | None:
        return None

    def idle(self) -> RetainedNative:
        raise NativePiUnavailable("Selected idle Pi child is unavailable or stale")

    async def inspect_context(self, persistent):
        """Observe acquired custody; browsing cannot acquire a native writer."""
        raise NativePiUnavailable("Native context requires an acquired native child")

    async def expected(self, launch, require_input_id) -> NativeAttestation:
        return launch.session.attestation()

    def retire(self, successor: NativeCustody | None = None) -> NativeCustody:
        return successor if successor is not None else self

    async def closed(self) -> NativeCustody:
        return self

    def reopen(self, identity: NativeSessionIdentity) -> ReopenNative:
        return ReopenNative(identity)


class EmptyNative(NativeCustody):
    available = False


class NativeCleanupFailed(RuntimeError):
    """The exact child is gone; its failed cleanup cannot own future inputs."""

    def __init__(self, successor, error):
        self.successor = successor
        super().__init__(
            f"Native process retired, but cleanup failed: {type(error).__name__}: {error}"
        )


@dataclass
class ReopenNative(NativeCustody):
    available = False
    identity: NativeSessionIdentity

    async def expected(self, launch, require_input_id):
        if not require_input_id:
            raise SavedSessionReopenError(
                "Saved native session requires explicit validated reopen."
            )
        try:
            launch.session.attest(self.identity)
        except ValueError as error:
            raise SavedSessionReopenError(
                "Saved native session failed strict reopen validation."
            ) from error
        return PendingAttestation(self.identity)

    def reopen(self, identity: NativeSessionIdentity) -> ReopenNative:
        self.identity.require_same_session(identity)
        return self


@dataclass
class RetiringNative(NativeCustody):
    available = False
    task: asyncio.Task[None]
    successor: NativeCustody
    child: PiSessionChild

    def retire(self, successor=None):
        if successor is not None:
            self.successor = successor
        return self

    async def closed(self):
        try:
            await asyncio.shield(self.task)
        except Exception as error:
            if self.child.proc.retired:
                raise NativeCleanupFailed(self.successor, error) from error
            raise
        return self.successor

    def reopen(self, identity):
        return self.successor.reopen(identity)


@dataclass
class BorrowedNative(NativeCustody):
    child: PiSessionChild
    successor: NativeCustody
    available = True

    async def inspect_context(self, persistent):
        from .pi_commands import AgentCommsInspectContext
        # The active TurnSession owns receive/correlation. Borrow its original
        # pending response instead of starting a competing reader.
        async with AgentCommsInspectContext().pending_response(
            self.child.reader,self.child.proc.stdin
        ) as response:
            return (await response).data.require_payload()

    def retire(self, successor=None):
        return RetiringNative(
            asyncio.create_task(self.child.close()),
            self.successor if successor is None else successor,
            self.child,
        )

    def reopen(self, identity):
        observed = self.child.attestation.require_identity()
        observed.require_same_session(identity)
        return ReopenNative(observed)


@dataclass
class RetainedNative(NativeCustody):
    retained = True
    child: PiSessionChild
    identity: NativeSessionIdentity
    revision: SessionRevision
    available = True

    @property
    def current(self) -> bool:
        return self.child.proc.alive() and self.revision.current(self.identity.session_file)

    def idle(self):
        if not self.current:
            return super().idle()
        return self

    def selected(self, identity: NativeSessionIdentity, package: Path) -> RetainedNative:
        if not self.identity.same_session(identity) or self.child.key[0].package != package:
            raise NativePiUnavailable("Selected idle Pi child is unavailable or stale")
        return self.idle()

    async def reload(self, witness: NativeWitness, operation: CompactionOperation,
                     reason: type[CompactionReason]) -> None:
        """Install this exact known commit through the acquired compaction owner.

        The caller owns PersistentPiSession.external_write and its borrow lock.
        The original manager opens and reconciles the real disk source, closes
        its old entry store, and installs context through the native compaction
        hook. Its SDK runtime, services and extension bindings remain acquired.
        """
        from .owner_compaction_prepare import PrepareCompactionHelper
        from .pi_commands import AgentCommsRestoreCompaction
        from .native_compaction_request import NativeIntent
        from .selected_source import SessionRevision
        from .session_fence import session_writer_fence

        witness.require_same_session(self.identity)
        # Use the existing whole-history preparation budget, not the cheap
        # settings-query deadline. A refusal never authorizes another attempt.
        async with asyncio.timeout(PrepareCompactionHelper.timeout_seconds):
            # The commit's global resource has closed. Reload needs only the
            # original per-session writer scope, in the same borrow->writer
            # order used for normal execution, not another global BUS grant.
            async with session_writer_fence(self.identity.session_file):
                witness.require_current_file(self.identity.path)
                request = AgentCommsRestoreCompaction(
                    reconciliation=NativeIntent.read(operation).reconciliation(operation.commit_id),
                    expected=operation.committed_outcome(), reason=reason, id=uuid4().hex,
                )
                response = await request.exchange(self.child.reader, self.child.proc.stdin, strict=True)
                response.require_request(request)
                attestation = PendingAttestation(self.identity)
                response = await attestation.request.exchange(
                    self.child.reader, self.child.proc.stdin, strict=True,
                )
                observed = attestation.accept(response)
                witness.require_current_file(self.identity.path)
                self.revision = SessionRevision.observe(self.identity.session_file).require_available()
                self.child.attestation = observed

    async def inspect_context(self, persistent):
        from .pi_commands import AgentCommsInspectContext
        async with persistent.lock:
            current = persistent.custody.idle()
            response = await AgentCommsInspectContext().exchange(
                current.child.reader,current.child.proc.stdin
            )
            return response.data.require_payload()

    def reuse(self, key):
        if self.child.key == key and self.current:
            self.child.attestation = PendingAttestation(self.identity)
            return self.child
        return None

    def managed_launch(self, command, arguments, *, worktree, environment, session_file):
        if self.current:
            original, authentication = self.child.key
            candidate = original.retained_managed(command, arguments,
                worktree=worktree, environment=environment, session_file=session_file)
            if candidate is not None and (candidate, candidate.configuration.auth_revision()) == (
                original, authentication
            ):
                return original
        return super().managed_launch(command, arguments, worktree=worktree,
            environment=environment, session_file=session_file)

    def retire(self, successor=None):
        return RetiringNative(
            asyncio.create_task(self.child.close()),
            EmptyNative() if successor is None else successor,
            self.child,
        )

    def reopen(self, identity):
        self.identity.require_same_session(identity)
        return ReopenNative(self.identity)
