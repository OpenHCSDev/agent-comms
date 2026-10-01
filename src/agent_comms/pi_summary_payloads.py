"""Strict selected-child RPC records; observations never grant input admission."""

from __future__ import annotations

import struct
from abc import abstractmethod
from dataclasses import dataclass, field
from typing import Literal

from .field_codec import FieldCodec
from .owner_compaction_prepare import NativeWitness
from .owner_compaction_settings import PiCompactionSettings
from .pi_payloads import PiCost, PiPayload, PiResponseData, PiUsage

@dataclass(frozen=True)
class SelectedModel(PiPayload):
    strict_fields = True
    provider: str
    model_id: str = field(metadata={"wire_name": "modelId"})
    context_window: int = field(metadata={"wire_name": "contextWindow"})

    def __post_init__(self):
        FieldCodec.decode(str, self.provider)
        FieldCodec.decode(str, self.model_id)
        FieldCodec.decode(int, self.context_window)
        if not self.provider or not self.model_id:
            raise ValueError("Exact selected model identity required")
        if not 0 < self.context_window <= 2**53 - 1:
            raise ValueError("Exact bounded selected model required")



@dataclass(frozen=True)
class SummaryFiles(PiPayload):
    strict_fields = True
    read_files: tuple[str, ...] = field(metadata={"wire_name": "readFiles"})
    modified_files: tuple[str, ...] = field(metadata={"wire_name": "modifiedFiles"})

    def __post_init__(self):
        for paths in (self.read_files, self.modified_files):
            if any(not p or "\0" in p or len(p.encode()) > 4096 for p in paths):
                raise ValueError("Invalid selected native file operations")

    def commit_metadata(self) -> list[list[str]]:
        return [
            [path.encode("utf-8").hex() for path in paths]
            for paths in (self.read_files, self.modified_files)
        ]


@dataclass(frozen=True)
class SummaryCost(PiCost):
    strict_fields = True

    def __post_init__(self):
        if any(
            v is None or not 0 <= v <= 2**53 - 1
            for v in (self.input, self.output, self.cache_read, self.cache_write, self.total)
        ):
            raise ValueError("Invalid selected native cost")

    def commit_metadata(self) -> list[str]:
        return [
            struct.pack(">d", float(value)).hex()
            for value in (
                self.input,
                self.output,
                self.cache_read,
                self.cache_write,
                self.total,
            )
        ]


@dataclass(frozen=True)
class SummaryUsage(PiUsage):
    strict_fields = True
    cost: SummaryCost | None = None

    def __post_init__(self):
        if (
            self.cost is None
            or any(
                v is None or not 0 <= v <= 2**53 - 1
                for v in (
                    self.input,
                    self.output,
                    self.cache_read,
                    self.cache_write,
                    self.total_tokens,
                )
            )
            or any(
                v is not None and not 0 <= v <= 2**53 - 1
                for v in (self.reasoning, self.cache_write_1h)
            )
        ):
            raise ValueError("Invalid selected native usage")

    def commit_metadata(self) -> list[int | str | None]:
        return [
            self.input,
            self.output,
            self.cache_read,
            self.cache_write,
            self.total_tokens,
            self.reasoning,
            self.cache_write_1h,
            *self.cost.commit_metadata(),
        ]


@dataclass(frozen=True)
class SummaryResult(PiPayload):
    strict_fields = True
    summary: str
    first_kept_entry_id: str = field(metadata={"wire_name": "firstKeptEntryId"})
    tokens_before: int = field(metadata={"wire_name": "tokensBefore"})
    details: SummaryFiles
    usage: SummaryUsage

    def __post_init__(self):
        if not self.summary.strip():
            raise ValueError("Invalid selected native summary")
        self.summary.encode("utf-8")  # Validate text, without a second output-size policy.


@dataclass(frozen=True, kw_only=True)
class SelectedSummaryData(PiResponseData):
    strict_fields = True
    version: Literal[1]
    operation_id: str = field(metadata={"wire_name": "operationId"})

    def require_request(self, request):
        if self.operation_id != request.operation_id:
            raise ValueError("Selected summary belongs to another operation")
        return self

    @abstractmethod
    def response(self, request, tokens_before):
        """Interpret this native outcome without granting commit or replay authority."""

    def settle(self, journal):
        """A complete summary stays reserved until the canonical writer commits it."""

    def require_result(self):
        return self

    def manual_summary(self, journal):
        raise ValueError("Selected summary has no manual result")

    def adaptive_summary(self, journal, identity):
        raise ValueError("Selected summary has no adaptive result")

    @classmethod
    def wire_member(cls, value):
        # Status is the native discriminator, mapped through the existing family.
        return cls.decode("summary_" + str(value.get("status")))


@dataclass(frozen=True, kw_only=True)
class SummaryDeclinedData(SelectedSummaryData, declared_name="summary_declined"):
    status: Literal["declined"]
    reason: str

    def response(self, request, tokens_before):
        return self

    def settle(self, journal):
        if self.reason not in {"split_turn", "unsupported"}:
            journal.summaries.refuse(self.operation_id, self.reason)

    def manual_summary(self, journal):
        journal.summaries.refuse(self.operation_id, self.reason)
        raise ValueError(f"Selected Pi declined manual summary ({self.reason})")

    def adaptive_summary(self, journal, identity):
        from .owner_compaction_runtime import SelectedSummaryDecline
        from .owner_compaction_settings import PiSettingsEvidenceError

        if self.reason in {"split_turn", "unsupported"}:
            return SelectedSummaryDecline(
                journal.summaries.get(self.operation_id), identity, self.reason
            )
        raise PiSettingsEvidenceError(
            f"Selected Pi declined summary ({self.reason}); original remains unbound"
        )

    def __post_init__(self):
        if not 0 < len(self.reason) <= 256:
            raise ValueError("Invalid summary decline reason")


@dataclass(frozen=True, kw_only=True)
class SummaryUnknownData(SelectedSummaryData, declared_name="summary_unknown"):
    status: Literal["unknown"]
    reason: str | None = None

    def response(self, request, tokens_before):
        from .selected_pi_summary_rpc import SelectedChildUnknown

        detail = (
            f"Selected summary failed: {self.reason} (outcome uncertain; input not retried)"
            if self.reason is not None
            else "Selected summary outcome is uncertain; native child supplied no failure detail"
        )
        raise SelectedChildUnknown(detail)

    def __post_init__(self):
        if self.reason is not None and (
            not 0 < len(self.reason) <= 1024
            or any(ord(c) < 32 or ord(c) == 127 for c in self.reason)
        ):
            raise ValueError("Invalid selected summary failure detail")


class WitnessedSummaryData(SelectedSummaryData):
    """Both known outcomes carry the same source custody and selected settings."""

    def require_source(self, request):
        if (
            self.witness != request.witness
            or self.selected != request.selected
            or self.settings != request.settings
        ):
            raise ValueError("Selected summary outcome unknown")


@dataclass(frozen=True, kw_only=True)
class SummarySummarizedData(WitnessedSummaryData, declared_name="summary_summarized"):
    status: Literal["summarized"]
    witness: NativeWitness
    selected: SelectedModel
    settings: PiCompactionSettings
    result: SummaryResult

    def response(self, request, tokens_before):
        self.require_source(request)
        if (
            self.result.first_kept_entry_id != request.witness.first_kept_entry_id
            or self.result.tokens_before != tokens_before
        ):
            raise ValueError("Selected summary result source changed")
        return self

    def manual_summary(self, journal):
        from .owner_compaction_manual import ManualSelectedSummary

        return ManualSelectedSummary(
            self.result.summary,
            self.result.details,
            self.result.usage,
            journal.summaries.get(self.operation_id),
        )

    def adaptive_summary(self, journal, identity):
        from .owner_compaction_runtime import SelectedNativeSummary

        return SelectedNativeSummary(
            self.result.summary,
            self.result.details,
            self.result.usage,
            journal.summaries.get(self.operation_id),
            identity,
        )


@dataclass(frozen=True, kw_only=True)
class SummaryFailedData(WitnessedSummaryData, declared_name="summary_failed"):
    """Joined provider failure with unchanged summary-only native source."""

    status: Literal["failed"]
    witness: NativeWitness
    selected: SelectedModel
    settings: PiCompactionSettings
    reason: str

    def response(self, request, tokens_before):
        self.require_source(request)
        return self

    def settle(self, journal):
        journal.summaries.fail(self.operation_id, self.reason)

    def require_result(self):
        from .selected_pi_summary_rpc import SelectedSummaryFailed

        raise SelectedSummaryFailed(self)

    def __post_init__(self):
        if not 0 < len(self.reason) <= 1024 or any(
            ord(c) < 32 or ord(c) == 127 for c in self.reason
        ):
            raise ValueError("Invalid selected summary failure detail")


@dataclass(frozen=True)
class CompactionSettingsData(PiResponseData):
    from .owner_compaction_settings import PiCompactionDecision

    strict_fields = True
    version: Literal[1]
    session_id: str = field(metadata={"wire_name": "sessionId"})
    session_file: str = field(metadata={"wire_name": "sessionFile"})
    selected: SelectedModel
    decision: PiCompactionDecision

    def require_request(self, request):
        from .native_session_reopen import NativeSessionIdentity

        original = NativeSessionIdentity(request.session_id, request.session_file)
        observed = NativeSessionIdentity(self.session_id, self.session_file)
        if observed != original or self.selected != request.selected:
            raise ValueError("Selected settings source changed")
        return self.decision
