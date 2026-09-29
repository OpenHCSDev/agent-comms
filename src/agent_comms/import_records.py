"""Imported external cases decoded at ingress, with no raw-record mirror."""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass

from .declared_family import DeclaredFamily
from .importing import ImportBuffer, ImportRole, ReverseImportBuffer, object_value, objects, text


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


@dataclass(frozen=True)
class IgnoredCodexItem(CodexItem):
    @classmethod
    def capture(cls, wire):
        return cls()

    def apply(self, buffer):
        pass


@dataclass(frozen=True)
class MessageCodexItem(CodexItem):
    role: ImportRole
    body: str
    source_id: str

    @classmethod
    def capture(cls, wire):
        role = wire.get("role")
        if role not in {"user", "assistant"}:
            return IgnoredCodexItem()
        content = wire.get("content")
        pieces = (
            [content]
            if isinstance(content, str)
            else [
                text(part.get("text"))
                for part in objects(content)
                if part.get("type") in {"input_text", "output_text", "text"}
            ]
        )
        return cls(ImportRole(role), "\n".join(pieces), text(wire.get("id")))

    def apply(self, buffer):
        buffer.add(self.role, self.body, self.source_id)


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
        if scan.checkpoint is None and not scan.latest_project:
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

    def reverse(self, scan, offset, raw):
        request = self.latest_request(scan.reverse_buffer.limits)
        if scan.checkpoint is not None:
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
        if scan.checkpoint is not None:
            probe = ReverseImportBuffer(scan.reverse_buffer.limits)
            self.item.apply(probe)
            scan.prior_request = probe.latest_request
            return bool(scan.prior_request)
        self.item.apply(scan.reverse_buffer)
        return False

    def forward(self, scan, buffer):
        self.item.apply(buffer)


@dataclass
class CodexImportScan:
    reverse_buffer: ReverseImportBuffer
    identity: str = ""
    project: str = ""
    latest_project: str = ""
    checkpoint: tuple[int, int, CompactedCodexRecord] | None = None
    prior_request: str = ""


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
