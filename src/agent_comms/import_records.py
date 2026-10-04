"""Imported external cases decoded at ingress, with no raw-record mirror."""

from __future__ import annotations

import hashlib
import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from .declared_family import DeclaredFamily
from .importing import ImportBuffer, ImportRole, ReverseImportBuffer, object_value, objects, text
from .turn_context import CodexRolloutProvenance


class ImportedCase(ABC):
    @classmethod
    def from_wire(cls, wire):
        try:
            member = cls.decode(wire.get("type"))
        except ValueError:
            member = cls.ignored_case()
        return member.capture(wire)

    @classmethod
    @abstractmethod
    def ignored_case(cls): ...

    @classmethod
    @abstractmethod
    def capture(cls, wire): ...


class OpenCodePart(ImportedCase, DeclaredFamily, affix="OpenCodePart"):
    @classmethod
    def ignored_case(cls):
        return IgnoredOpenCodePart

    @abstractmethod
    def apply(self, buffer, role, source_id, summary): ...


@dataclass(frozen=True)
class IgnoredOpenCodePart(OpenCodePart):
    @classmethod
    def capture(cls, wire):
        return cls()

    def apply(self, buffer, role, source_id, summary):
        pass


@dataclass(frozen=True)
class TextOpenCodePart(OpenCodePart):
    body: str
    ignored: bool

    @classmethod
    def capture(cls, wire):
        return cls(text(wire.get("text")), bool(wire.get("ignored")))

    def apply(self, buffer, role, source_id, summary):
        if not self.ignored:
            if summary:
                buffer.set_summary(self.body)
            else:
                buffer.add(role, self.body, source_id)


@dataclass(frozen=True)
class ToolOpenCodePart(OpenCodePart):
    body: str
    source_id: str

    @classmethod
    def capture(cls, wire):
        state = object_value(wire.get("state", {}))
        status = text(state.get("status"))
        body = f"[Historical tool {text(wire.get('tool'))} — {status}]\nInput: " + json.dumps(
            state.get("input"), ensure_ascii=False
        )
        output = state.get("output") if status == "completed" else state.get("error")
        if output is not None:
            body += "\nResult: " + (output if isinstance(output, str) else json.dumps(output))
        return cls(body, text(wire.get("callID")))

    def apply(self, buffer, role, source_id, summary):
        buffer.add(ImportRole.TOOL, self.body, self.source_id)


@dataclass(frozen=True)
class FileOpenCodePart(OpenCodePart):
    reference: str

    @classmethod
    def capture(cls, wire):
        return cls(text(wire.get("filename")) or text(wire.get("mime")))

    def apply(self, buffer, role, source_id, summary):
        buffer.notices.add("Attachment contents are not imported; source references are retained.")
        buffer.add(ImportRole.TOOL, f"[Historical attachment: {self.reference}]", source_id)


class CodexItem(ImportedCase, DeclaredFamily, affix="CodexItem"):
    @classmethod
    def ignored_case(cls):
        return IgnoredCodexItem

    @abstractmethod
    def apply(self, buffer): ...

    def historical_instructions(self):
        return ()


@dataclass(frozen=True)
class IgnoredCodexItem(CodexItem):
    @classmethod
    def capture(cls, wire):
        return cls()

    def apply(self, buffer):
        pass


@dataclass(frozen=True)
class MessageCodexItem(CodexItem):
    text_types = frozenset({"input_text", "output_text", "text"})
    role: ImportRole
    body: str
    source_id: str

    @classmethod
    def capture(cls, wire):
        role = wire.get("role")
        if role not in {"user", "assistant", "system", "developer"}:
            return IgnoredCodexItem()
        content = wire.get("content")
        pieces = (
            [content]
            if isinstance(content, str)
            else [
                text(part.get("text"))
                for part in objects(content)
                if part.get("type") in cls.text_types
            ]
        )
        body = "\n".join(pieces)
        if role in {"system", "developer"}:
            return HistoricalInstructionCodexItem(role, body)
        return cls(ImportRole(role), body, text(wire.get("id")))

    def apply(self, buffer):
        buffer.add(self.role, self.body, self.source_id)


@dataclass(frozen=True)
class HistoricalInstructionCodexItem(CodexItem):
    """Original external instruction wording, excluded from portable messages."""

    role: str
    body: str

    @classmethod
    def capture(cls, wire):
        # Only the original message decoder constructs this historical case.
        return IgnoredCodexItem()

    def apply(self, buffer):
        pass

    def historical_instructions(self):
        return (self,)


@dataclass(frozen=True)
class FunctionCallCodexItem(CodexItem):
    body: str
    source_id: str

    @classmethod
    def capture(cls, wire):
        return cls(
            f"[Historical tool call: {text(wire.get('name'))}]\n"
            + text(wire.get("arguments", wire.get("input"))),
            text(wire.get("call_id")),
        )

    def apply(self, buffer):
        buffer.add(ImportRole.TOOL, self.body, self.source_id)


class CustomToolCallCodexItem(FunctionCallCodexItem):
    pass


@dataclass(frozen=True)
class FunctionCallOutputCodexItem(CodexItem):
    body: str
    source_id: str

    @classmethod
    def capture(cls, wire):
        output = wire.get("output")
        body = output if isinstance(output, str) else json.dumps(output, ensure_ascii=False)
        return cls("[Historical tool result]\n" + body, text(wire.get("call_id")))

    def apply(self, buffer):
        buffer.add(ImportRole.TOOL, self.body, self.source_id)


class CustomToolCallOutputCodexItem(FunctionCallOutputCodexItem):
    pass


class CodexRecord(ImportedCase, DeclaredFamily, affix="CodexRecord"):
    @classmethod
    def from_wire(cls, wire):
        # Validate the external envelope once, including ignored future records.
        payload = object_value(wire.get("payload", {}))
        try:
            member = cls.decode(wire.get("type"))
        except ValueError:
            member = IgnoredCodexRecord
        return member.capture(payload)

    @classmethod
    def ignored_case(cls):
        return IgnoredCodexRecord

    def header(self, scan):
        return False

    def reverse(self, scan, offset, raw):
        return False

    def forward(self, scan, buffer):
        pass

    def historical_instructions(self):
        return ()

    def instruction_sources(self, source, offset, raw):
        digest = hashlib.sha256(raw).hexdigest()
        return tuple(CodexRolloutProvenance(str(source.resolve()), offset, len(raw), digest,
                                           index, instruction.role)
                     for index, instruction in enumerate(self.historical_instructions()))


@dataclass(frozen=True)
class IgnoredCodexRecord(CodexRecord):
    @classmethod
    def capture(cls, payload):
        return cls()


@dataclass(frozen=True)
class SessionMetaCodexRecord(CodexRecord):
    identity: str
    project: str

    @classmethod
    def capture(cls, payload):
        return cls(text(payload.get("id")), text(payload.get("cwd")))

    def header(self, scan):
        scan.identity, scan.project = self.identity, self.project
        return True


@dataclass(frozen=True)
class TurnContextCodexRecord(CodexRecord):
    project: str

    @classmethod
    def capture(cls, payload):
        return cls(text(payload.get("cwd")))

    def reverse(self, scan, offset, raw):
        if scan.searching_checkpoint and scan.needs_project:
            scan.latest_project = self.project
        return False

    def forward(self, scan, buffer):
        scan.latest_project = self.project or scan.latest_project


@dataclass(frozen=True)
class CompactedCodexRecord(CodexRecord):
    summary: str
    replacement: tuple[CodexItem, ...]
    guardian: bool

    @classmethod
    def capture(cls, payload):
        return cls(
            text(payload.get("message")) or text(payload.get("summary")),
            tuple(
                CodexItem.from_wire(item) for item in objects(payload.get("replacement_history"))
            ),
            bool(tuple(objects(payload.get("guardian_history")))),
        )

    def latest_request(self, limits):
        probe = ImportBuffer(limits)
        for item in self.replacement:
            item.apply(probe)
        return probe.latest_request

    def historical_instructions(self):
        return tuple(instruction for item in self.replacement
                     for instruction in item.historical_instructions())

    def reverse(self, scan, offset, raw):
        request = self.latest_request(scan.reverse_buffer.limits)
        if scan.found_checkpoint:
            scan.prior_request = request
            return bool(request)
        scan.checkpoint = (offset, len(raw), self)
        return bool(scan.reverse_buffer.latest_request or request)

    def forward(self, scan, buffer):
        buffer.set_summary(self.summary)
        for item in self.replacement:
            item.apply(buffer)
        if self.guardian:
            buffer.notices.add("Codex guardian history is source-private and was not imported.")


@dataclass(frozen=True)
class ResponseItemCodexRecord(CodexRecord):
    item: CodexItem

    @classmethod
    def capture(cls, payload):
        return cls(CodexItem.from_wire(payload))

    def reverse(self, scan, offset, raw):
        if scan.found_checkpoint:
            probe = ReverseImportBuffer(scan.reverse_buffer.limits)
            self.item.apply(probe)
            scan.prior_request = probe.latest_request
            return bool(scan.prior_request)
        self.item.apply(scan.reverse_buffer)
        return False

    def forward(self, scan, buffer):
        self.item.apply(buffer)

    def historical_instructions(self):
        return self.item.historical_instructions()


@dataclass
class CodexImportScan:
    reverse_buffer: ReverseImportBuffer
    identity: str = ""
    project: str = ""
    latest_project: str = ""
    checkpoint: tuple[int, int, CompactedCodexRecord] | None = None
    prior_request: str = ""
    instruction_sources: set[CodexRolloutProvenance] = field(default_factory=set)

    def reverse_record(self, record, source, offset, raw):
        # Older records searched only for a missing user request cannot acquire
        # instruction membership in the selected checkpoint/suffix.
        if self.searching_checkpoint:
            self.instruction_sources.update(record.instruction_sources(source, offset, raw))
        return record.reverse(self, offset, raw)

    def forward_record(self, record, source, offset, raw, buffer):
        self.instruction_sources.update(record.instruction_sources(source, offset, raw))
        record.forward(self, buffer)

    @property
    def historical_instructions(self):
        return tuple(sorted(self.instruction_sources,
                            key=lambda value:(value.offset,value.instruction)))

    @property
    def needs_project(self):
        return not self.latest_project

    @property
    def found_checkpoint(self):
        return self.checkpoint is not None

    @property
    def searching_checkpoint(self):
        return self.checkpoint is None

    def rebuild(self):
        if self.checkpoint is None:
            return self.reverse_buffer.forward_buffer(), None
        buffer = ImportBuffer(self.reverse_buffer.limits, notices=set(self.reverse_buffer.notices))
        offset, size, checkpoint = self.checkpoint
        checkpoint.forward(self, buffer)
        return buffer, offset + size


class OpenCodeSource(DeclaredFamily, affix="OpenCodeSource"):
    suffixes = frozenset()

    @classmethod
    def for_path(cls, source):
        return next(
            (member for member in cls.members_with(cls) if source.suffix in member.suffixes),
            ExportOpenCodeSource,
        )

    @classmethod
    @abstractmethod
    def read(cls, importer, source, buffer, session_id): ...


class DatabaseOpenCodeSource(OpenCodeSource):
    suffixes = frozenset({".db", ".sqlite", ".sqlite3"})

    @classmethod
    def read(cls, importer, source, buffer, session_id):
        return importer._database(source, buffer, session_id)


class ExportOpenCodeSource(OpenCodeSource):
    @classmethod
    def read(cls, importer, source, buffer, session_id):
        return importer._export(source, buffer, session_id)
