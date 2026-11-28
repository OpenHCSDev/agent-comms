"""The files a message carries: what a sender uploads, the record each reader lists, and the
store that keeps both.

A message's envelope never names its files. They are rows keyed by the message's reference,
so a reader that predates them decodes every message, and every page, unchanged. The bytes
are stored once per SHA-256, which only deduplicates storage: a reader reaches a file
through a message it may read, never through its digest.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path, PurePath
from typing import Annotated, ClassVar

from agent_comms.messaging.message_reference import MessageReference
from agent_comms.nominal.field_codec import FieldCodec, MessageId, WireName, WireOmitDefault
from agent_comms.storage.typed_table import (
    Column,
    DurableStore,
    ForeignKey,
    StoreRows,
    TypedTable,
    WithoutRowid,
)
from agent_comms.transport.request import tool_field
from agent_comms.transport.tool_result import ToolResult
from agent_comms_hooks import ToolRefusedError

MAX_MESSAGE_FILE_MIB = 32
MAX_MESSAGE_FILE_BYTES = MAX_MESSAGE_FILE_MIB * 1024 * 1024
"""The most file bytes one message carries, all its files together."""

MAX_FILE_NAME_LENGTH = 255
REFUSED_NAME_CHARACTERS = frozenset("\\\0")
"""A backslash is a directory on Windows; NUL ends a path on every system."""

MIME_TYPE = re.compile(r"[a-z0-9][a-z0-9!#$&^_.+-]*/[a-z0-9][a-z0-9!#$&^_.+-]*")


def base64_length(size: int) -> int:
    """How many base64 characters encode ``size`` bytes."""
    return 4 * ((size + 2) // 3)


MAX_MESSAGE_FILE_TEXT = base64_length(MAX_MESSAGE_FILE_BYTES)
"""The most base64 text one message's files are sent as."""

MAX_SEND_REQUEST_BYTES = MAX_MESSAGE_FILE_TEXT + 1024 * 1024
"""The most bytes one request carrying a message and its files is posted as: the files'
text and a megabyte for the rest of the request."""


def require_file_name(name: str) -> None:
    """A file's name names one file and no directory: 1 to 255 characters, its own last path
    part, not only dots, and no backslash or NUL."""
    if not name.strip() or len(name) > MAX_FILE_NAME_LENGTH:
        raise ValueError("A file name is 1 to 255 characters.")
    if PurePath(name).name != name or not name.strip("."):
        raise ValueError("A file name names no directory.")
    if not REFUSED_NAME_CHARACTERS.isdisjoint(name):
        raise ValueError("A file name holds no backslash or NUL.")


@dataclass(frozen=True, slots=True)
class FileUpload:
    """One file a sender attaches: its name, its MIME type and its bytes as base64."""

    name: str
    mime_type: Annotated[str, WireName("mimeType")]
    data: str

    def __post_init__(self) -> None:
        require_file_name(self.name)
        if not MIME_TYPE.fullmatch(self.mime_type):
            raise ValueError(f"Unsupported file type: {self.mime_type!r}")
        if len(self.data) > MAX_MESSAGE_FILE_TEXT:
            raise ValueError(f"Files exceed the {MAX_MESSAGE_FILE_MIB} MiB message limit.")
        if not self.content:
            raise ValueError("File data is empty.")
        if self.size > MAX_MESSAGE_FILE_BYTES:
            raise ValueError(f"Files exceed the {MAX_MESSAGE_FILE_MIB} MiB message limit.")

    @classmethod
    def of(cls, name: str, mime_type: str, content: bytes) -> FileUpload:
        """The upload of bytes a picker returned, named by the last part of their name."""
        return cls(PurePath(name).name, mime_type, base64.b64encode(content).decode("ascii"))

    @property
    def content(self) -> bytes:
        try:
            return base64.b64decode(self.data, validate=True)
        except (ValueError, binascii.Error) as error:
            raise ValueError("File data must be valid base64.") from error

    @property
    def size(self) -> int:
        return len(self.data) * 3 // 4 - (len(self.data) - len(self.data.rstrip("=")))

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.content).hexdigest()

    @staticmethod
    def require_bounded(uploads: tuple[FileUpload, ...]) -> None:
        """One message's files fit its limit together."""
        if sum(upload.size for upload in uploads) > MAX_MESSAGE_FILE_BYTES:
            raise ValueError(f"Files exceed the {MAX_MESSAGE_FILE_MIB} MiB message limit.")


@dataclass(frozen=True, kw_only=True)
class SendsFiles:
    """A send that may carry files. An empty list is left off the wire, so a send with no
    files is the request a Core that predates files decodes."""

    files: Annotated[
        tuple[FileUpload, ...],
        *tool_field(
            f"Files the message carries: name, mimeType and base64 data; {MAX_MESSAGE_FILE_MIB} MiB in all"
        ),
        WireOmitDefault(),
    ] = ()


@dataclass(frozen=True, slots=True)
class MessageFile:
    """One file a message carries, as every reader lists it."""

    message: MessageReference
    position: int
    name: str
    mime_type: Annotated[str, WireName("mimeType")]
    size: int
    sha256: str

    def __post_init__(self) -> None:
        require_file_name(self.name)

    def mention_in(self, conversation: str) -> str:
        """How an agent's delivered message names it, with the schema-valid read arguments."""
        arguments = FieldCodec.compact(
            {
                "conversation": conversation,
                "message": FieldCodec.encode(self.message),
                "position": self.position,
            },
            ensure_ascii=True,
        )
        return (
            f"File {self.position}: {self.name} ({self.mime_type}, {self.size} bytes); "
            f"comms_message_file {arguments}"
        )


@dataclass(frozen=True, slots=True)
class MessageFilesRead(ToolResult):
    """The files of the messages a read named that its caller may read."""

    files: tuple[MessageFile, ...]


@dataclass(frozen=True, slots=True)
class MessageFileContent(ToolResult):
    """One message file and its bytes as base64."""

    file: MessageFile
    data: str


class MessageFilesRows(StoreRows):
    """The tables of MessageFiles; each row declares itself one."""


@dataclass(frozen=True, slots=True)
class FileContentRow(MessageFilesRows, WithoutRowid, TypedTable):
    """Bytes stored once per digest, as the base64 text the sender uploaded."""

    sha256: Annotated[str, Column(primary_key=True)]
    data: str


@dataclass(frozen=True, slots=True)
class MessageFileRow(MessageFilesRows, WithoutRowid, TypedTable):
    seq: Annotated[int, Column(primary_key=True)]
    position: Annotated[int, Column(primary_key=True)]
    message_id: MessageId
    name: str
    mime_type: str
    size: int
    sha256: str

    @classmethod
    def references(cls) -> tuple[ForeignKey, ...]:
        return (ForeignKey(columns=("sha256",), target=FileContentRow, target_columns=("sha256",)),)

    @property
    def file(self) -> MessageFile:
        return MessageFile(
            MessageReference(self.seq, self.message_id),
            self.position,
            self.name,
            self.mime_type,
            self.size,
            self.sha256,
        )


@dataclass(frozen=True, slots=True)
class MessageFiles(DurableStore):
    """Every message's files. Files belong to messages, which outlive thread declarations."""

    root: Path
    filename: ClassVar[str] = "message_files.sqlite3"
    tables = MessageFilesRows

    def keep(self, uploads: tuple[FileUpload, ...]) -> None:
        """Store the bytes before the message that carries them is published, so a failed
        write publishes nothing."""
        if not uploads:
            return
        FileUpload.require_bounded(uploads)
        with self.writing() as db:
            for upload in uploads:
                FileContentRow(upload.sha256, upload.data).insert_if_absent(db)

    def attach(self, message: MessageReference, uploads: tuple[FileUpload, ...]) -> None:
        """Name kept bytes as the files of a published message, in the order they were sent."""
        if not uploads:
            return
        with self.writing() as db:
            for position, upload in enumerate(uploads):
                MessageFileRow(
                    message.seq,
                    position,
                    message.message_id,
                    upload.name,
                    upload.mime_type,
                    upload.size,
                    upload.sha256,
                ).insert(db)

    def of(self, messages: Iterable[MessageReference]) -> tuple[MessageFile, ...]:
        """The files of these messages, in bus and send order."""
        references = frozenset(messages)
        if not references:
            return ()
        with self.snapshot() as db:
            rows = MessageFileRow.select(
                db,
                where="{seq} IN (" + ", ".join("?" for _ in references) + ")",
                parameters=tuple(reference.seq for reference in references),
                order_by=("seq", "position"),
            )
        return tuple(row.file for row in rows if row.file.message in references)

    def content(self, file: MessageFile) -> MessageFileContent:
        with self.snapshot() as db:
            (row,) = FileContentRow.select(db, sha256=file.sha256)
        return MessageFileContent(file, row.data)

    def require(self, message: MessageReference, position: int) -> MessageFile:
        for file in self.of((message,)):
            if file.position == position:
                return file
        raise ToolRefusedError(f"Message {message.name} has no file {position}.")
