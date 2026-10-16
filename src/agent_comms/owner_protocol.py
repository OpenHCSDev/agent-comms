"""The owner socket protocol a Core speaks, announced before anything else.

A frontend and a thread owner are the same component and change in lockstep.
A running owner can still predate the frontend's Core. The owner therefore
announces its protocol as the first line of every subscription, and the
frontend compares it once before decoding any update. A mismatch is a typed
incompatible owner, never a decode error from the first changed field.

This file is exported with ``export-subst`` (see ``.gitattributes``): ``git
archive`` writes the exported commit into ``_REVISION_STAMP``.
"""

from __future__ import annotations

import hashlib
import json
import re
from abc import abstractmethod
from dataclasses import dataclass
from functools import cache

from .acp_extension import UpdateBatch
from .declared_family import DeclaredFamily
from .field_codec import FieldCodec
from .runtime_requests import RuntimeRequest

_REVISION_STAMP = "$Format:%H$"


class CoreBuild(DeclaredFamily, affix="CoreBuild"):
    """Where this Core's code came from."""

    @classmethod
    @cache
    def current(cls) -> CoreBuild:
        if re.fullmatch(r"[0-9a-f]{40}", _REVISION_STAMP):
            return ExportedCoreBuild(_REVISION_STAMP)
        return SourceCoreBuild()

    @abstractmethod
    def label(self) -> str: ...


@dataclass(frozen=True)
class ExportedCoreBuild(CoreBuild):
    """A build from ``git archive`` of one commit."""

    revision: str

    def label(self) -> str:
        return f"Core {self.revision[:9]}"


@dataclass(frozen=True)
class SourceCoreBuild(CoreBuild):
    """A build straight from a source checkout; git recorded no commit."""

    def label(self) -> str:
        return "Core from a source checkout"


@dataclass(frozen=True)
class OwnerProtocol:
    """The digest of the declared owner socket schemas, with the build behind it.

    ``wire`` derives from the update and request declarations themselves, so it
    changes exactly when either side's encoding changes. This record is the
    handshake and must keep its shape.
    """

    wire: str
    build: CoreBuild

    @classmethod
    @cache
    def current(cls) -> OwnerProtocol:
        schema = json.dumps(
            [FieldCodec.record_schema(UpdateBatch), FieldCodec.value_schema(RuntimeRequest)],
            sort_keys=True,
        )
        return cls(hashlib.sha256(schema.encode()).hexdigest(), CoreBuild.current())

    def announcement(self) -> dict:
        return FieldCodec.encode(OwnerAnnouncement(self))

    def require_compatible(self, thread: str, message: dict) -> None:
        """Compare an owner's first subscription line with this Core, once."""
        if message.keys() != OwnerAnnouncement.wire_keys():
            raise IncompatibleOwnerError(UnannouncedOwner(thread=thread, frontend=self.build))
        owner = FieldCodec.decode(OwnerAnnouncement, message).owner
        if owner.wire != self.wire:
            raise IncompatibleOwnerError(
                OtherProtocolOwner(thread=thread, frontend=self.build, owner=owner.build)
            )


@dataclass(frozen=True)
class OwnerAnnouncement:
    """The first line of every owner subscription."""

    owner: OwnerProtocol

    @classmethod
    def wire_keys(cls) -> frozenset[str]:
        return frozenset(key for _, key in FieldCodec.record_fields(cls))


@dataclass(frozen=True, kw_only=True)
class IncompatibleOwner(DeclaredFamily, affix="Owner"):
    """A running owner whose protocol differs from the frontend's Core."""

    thread: str
    frontend: CoreBuild

    FAILURE = "agentCommsIncompatibleOwner"

    @classmethod
    def from_failure(cls, data: object) -> IncompatibleOwner | None:
        if not isinstance(data, dict) or cls.FAILURE not in data:
            return None
        return FieldCodec.decode(cls, data.get(cls.FAILURE))

    def failure_metadata(self) -> dict:
        return {self.FAILURE: FieldCodec.encode(self)}

    def line(self) -> str:
        return (
            f"The {self.thread} owner runs {self.owner_label()}, which cannot talk to "
            f"this app's {self.frontend.label()}. It opens here after the owner restarts while idle."
        )

    @abstractmethod
    def owner_label(self) -> str: ...


@dataclass(frozen=True, kw_only=True)
class UnannouncedOwner(IncompatibleOwner):
    """The owner sent no protocol: its Core predates the announcement."""

    def owner_label(self) -> str:
        return "a Core older than protocol announcements"


@dataclass(frozen=True, kw_only=True)
class OtherProtocolOwner(IncompatibleOwner):
    owner: CoreBuild

    def owner_label(self) -> str:
        return self.owner.label()


class IncompatibleOwnerError(Exception):
    """Carries the typed state; not a RuntimeError, so no reconnect loop retries it."""

    def __init__(self, owner: IncompatibleOwner) -> None:
        super().__init__(owner.line())
        self.owner = owner
