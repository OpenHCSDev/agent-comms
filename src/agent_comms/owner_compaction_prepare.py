"""Read-only, pinned native preparation before any adaptive summary request.

Only native witness and bounded preparation metadata cross this boundary; no
summary source, prompt, or session content is returned to the caller. This is
not a provider invocation or a native writer. OwnerCompactionCommit must still
capture its canonical source BEFORE summary generation and recheck on commit.
"""

from __future__ import annotations

import asyncio
import os
import stat
from abc import abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Self

from .declared_family import DeclaredFamily
from .native_package import verify_native_package
from .native_session_reopen import NativeSessionIdentity
from .owner_compaction_settings import PiCompactionSettings
from .pi_helper import PiHelper, SessionHelperRequest
from .native_revision_text import NativeRevisionText
from .private_path import FileRevision

if TYPE_CHECKING:
    from .compaction_result import CompactionResult, RefusedCompactionResult
    from .native_entries import NativeEvidenceRead


class NativePreparationError(ValueError):
    """No safe preparation was established; never start a summary request."""


@dataclass(frozen=True)
class NativeWitness(NativeSessionIdentity):
    """One decoded native cutpoint; later owner/disk CAS remains independent."""

    leaf_id: str = field(metadata={"wire_name": "leafId"})
    first_kept_entry_id: str = field(metadata={"wire_name": "firstKeptEntryId"})
    revision: Annotated[FileRevision, NativeRevisionText]

    def __post_init__(self):
        super().__post_init__()
        if not self.leaf_id or not self.first_kept_entry_id:
            raise NativePreparationError("Exact native witness required")

    def require_current_file(self, file: Path) -> None:
        self.require_session(str(file))
        if self.revision != FileRevision.from_stat(file.stat()):
            raise ValueError("Native retained source changed since preparation")

    def retained_task_facts(self, reader: NativeEvidenceRead | None = None):
        from .native_entries import NativeEvidenceRead

        with NativeEvidenceRead.borrow(Path(self.session_file), reader) as evidence:
            return evidence.retained_task_facts(self)


class NativePreparationResult(DeclaredFamily, affix="PreparationResult"):
    family_discriminator = "status"

    def at_complete_boundary(self) -> NativePreparationResult:
        return self

    @abstractmethod
    def checked(self, file: Path, revision: FileRevision) -> Self:
        """Bind an observed cutpoint to the already captured native revision."""

    @abstractmethod
    def require_ready(self) -> NativePreparation:
        """A captured source must retain a ready native cut, never a skip."""

    @abstractmethod
    async def compact_owner(
        self, perform: Callable[[NativePreparation], Awaitable[CompactionResult]]
    ) -> CompactionResult:
        """Only a ready preparation enters the existing owner operation."""


@dataclass(frozen=True)
class SkipPreparationResult(NativePreparationResult):
    session_id: str = field(metadata={"wire_name": "sessionId"})

    def checked(self, file: Path, revision: FileRevision) -> Self:
        if not self.session_id:
            raise NativePreparationError("Native session identity missing")
        return self

    def require_ready(self) -> NativePreparation:
        raise NativePreparationError("Captured compaction source lost its native preparation")

    async def compact_owner(self, perform) -> RefusedCompactionResult:
        from .compaction_result import RefusedCompactionResult

        return RefusedCompactionResult("Selected native history has no complete safe compaction cut")


@dataclass(frozen=True)
class NativePreparation(NativePreparationResult, declared_name="ready"):
    witness: NativeWitness
    tokens_before: int = field(metadata={"wire_name": "tokensBefore"})
    is_split_turn: bool = field(metadata={"wire_name": "isSplitTurn"})

    def __post_init__(self):
        if not 0 <= self.tokens_before <= 2**53 - 1:
            raise NativePreparationError("Invalid native preparation token count")

    def checked(self, file: Path, revision: FileRevision) -> NativePreparation:
        if self.witness.session_file != str(file) or self.witness.revision != revision:
            raise NativePreparationError("Invalid native witness")
        return self

    def require_ready(self) -> NativePreparation:
        return self

    def at_complete_boundary(self) -> NativePreparationResult:
        if self.is_split_turn:
            return SkipPreparationResult(self.witness.session_id)
        return self

    async def compact_owner(self, perform):
        return await perform(self)


@dataclass(frozen=True)
class PreparationRequest(SessionHelperRequest):
    settings: PiCompactionSettings
    context_window: int
    retained_text: str

    def __post_init__(self):
        if type(self.context_window) is not int or not 0 < self.context_window <= 2**53 - 1:
            raise NativePreparationError("Exact selected context window required")


class PrepareCompactionHelper(PiHelper):
    script = Path(__file__).with_name("_pi_helpers") / "prepare_compaction.mjs"
    request = PreparationRequest
    result = NativePreparationResult


def prepare_native_source(
    package: Path, session_file: str, *, settings: PiCompactionSettings, context_window: int,
    retained_text: str = "",
) -> NativePreparationResult:
    """Derive Pi's actual cut point without a model call or a session mutation.

    The caller supplies the actual selected window and effective settings.
    The returned witness is not authority: the owner captures source and the
    writer later CASes on disk.
    """
    try:
        package = package.resolve(strict=True)
        verify_native_package(package)
        file = Path(session_file).absolute()
        if file != file.resolve(strict=True):
            raise NativePreparationError("Native session path is not canonical")
        before = file.stat()
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_uid not in (0, os.getuid())
            or before.st_size <= 0
        ):
            raise NativePreparationError("Native session is not regular storage")
        result = asyncio.run(
            PrepareCompactionHelper.run(
                PreparationRequest(
                    str(package),
                    str(file),
                    settings,
                    context_window,
                    retained_text,
                ),
                cwd=file.parent,
            )
        )
        after = file.stat()

        revision = FileRevision.from_stat(before)
        if revision != FileRevision.from_stat(after):
            raise NativePreparationError("Native preparation failed or changed")
        return result.checked(file, revision)
    except (OSError, ValueError, TypeError) as error:
        if isinstance(error, NativePreparationError):
            raise
        raise NativePreparationError("Native source cannot be prepared") from error
