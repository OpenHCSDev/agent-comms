"""Read-only, pinned native preparation before any adaptive summary request.

Only native witness and bounded preparation metadata cross this boundary; no
summary source, prompt, or session content is returned to the caller. This is
not a provider invocation or a native writer. OwnerCompactionCommit must still
capture its canonical source BEFORE summary generation and recheck on commit.
"""

from __future__ import annotations

import asyncio
import os
import re
import stat
from abc import abstractmethod
from dataclasses import dataclass, field, fields
from pathlib import Path

from .declared_family import DeclaredFamily
from .native_package import verify_native_package
from .owner_compaction_settings import PiCompactionSettings
from .pi_helper import PiHelper, SessionHelperRequest


class NativePreparationError(ValueError):
    """No safe preparation was established; never start a summary request."""


@dataclass(frozen=True)
class NativeWitness:
    """One decoded native cutpoint; later owner/disk CAS remains independent."""

    session_id: str = field(metadata={"wire_name": "sessionId"})
    session_file: str = field(metadata={"wire_name": "sessionFile"})
    leaf_id: str = field(metadata={"wire_name": "leafId"})
    first_kept_entry_id: str = field(metadata={"wire_name": "firstKeptEntryId"})
    revision: str

    def __post_init__(self):
        if any(not getattr(self, item.name) for item in fields(self)):
            raise NativePreparationError("Exact native witness required")
        if (
            not self.session_file.startswith("/")
            or re.fullmatch(r"[0-9]+:[0-9]+:[0-9]+:[0-9]+:[0-9]+", self.revision) is None
        ):
            raise NativePreparationError("Canonical native path and revision required")


class NativePreparationResult(DeclaredFamily, affix="PreparationResult"):
    family_discriminator = "status"

    @abstractmethod
    def checked(self, file: Path, revision: str) -> NativePreparation | None:
        """Bind an observed cutpoint to the already captured native revision."""


@dataclass(frozen=True)
class SkipPreparationResult(NativePreparationResult):
    session_id: str = field(metadata={"wire_name": "sessionId"})

    def checked(self, file: Path, revision: str) -> None:
        if not self.session_id:
            raise NativePreparationError("Native session identity missing")
        return None


@dataclass(frozen=True)
class NativePreparation(NativePreparationResult, declared_name="ready"):
    witness: NativeWitness
    tokens_before: int = field(metadata={"wire_name": "tokensBefore"})
    is_split_turn: bool = field(metadata={"wire_name": "isSplitTurn"})

    def __post_init__(self):
        if not 0 <= self.tokens_before <= 2**53 - 1:
            raise NativePreparationError("Invalid native preparation token count")

    def checked(self, file: Path, revision: str) -> NativePreparation:
        if self.witness.session_file != str(file) or self.witness.revision != revision:
            raise NativePreparationError("Invalid native witness")
        return self


@dataclass(frozen=True)
class PreparationRequest(SessionHelperRequest):
    settings: PiCompactionSettings | None


class PrepareCompactionHelper(PiHelper):
    script = Path(__file__).with_name("_pi_helpers") / "prepare_compaction.mjs"
    request = PreparationRequest
    result = NativePreparationResult


def prepare_native_source(
    package: Path, session_file: str, *, keep_recent_tokens: int | None = None
) -> NativePreparation | None:
    """Derive Pi's actual cut point without a model call or a session mutation.

    A test may explicitly override Pi's recent window; production defaults to
    Pi's declared DEFAULT_COMPACTION_SETTINGS. The returned witness is not
    authority: the owner captures source and the writer later CASes on disk.
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
            or not 0 < before.st_size <= 256 * 1024 * 1024
        ):
            raise NativePreparationError("Native session is not bounded regular storage")
        result = asyncio.run(
            PrepareCompactionHelper.run(
                PreparationRequest(
                    str(package),
                    str(file),
                    PiCompactionSettings(0, keep_recent_tokens)
                    if keep_recent_tokens is not None
                    else None,
                ),
                cwd=file.parent,
            )
        )
        after = file.stat()

        def revision(info: os.stat_result) -> tuple[int, ...]:
            return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)

        if revision(before) != revision(after):
            raise NativePreparationError("Native preparation failed or changed")
        return result.checked(file, ":".join(map(str, revision(before))))
    except (OSError, ValueError, TypeError) as error:
        if isinstance(error, NativePreparationError):
            raise
        raise NativePreparationError("Native source cannot be prepared") from error
