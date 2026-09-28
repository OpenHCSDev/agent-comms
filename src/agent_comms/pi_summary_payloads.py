"""Strict selected-child RPC records; observations never grant input admission."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

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
        if not self.provider or not self.model_id or not 0 < self.context_window <= 2**53 - 1:
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


@dataclass(frozen=True)
class SummaryCost(PiCost):
    strict_fields = True

    def __post_init__(self):
        if any(
            v is None or not 0 <= v <= 2**53 - 1
            for v in (self.input, self.output, self.cache_read, self.cache_write, self.total)
        ):
            raise ValueError("Invalid selected native cost")


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

    @classmethod
    def wire_member(cls, value):
        # Status is the native discriminator, mapped through the existing family.
        return cls.decode("summary_" + str(value.get("status")))


@dataclass(frozen=True, kw_only=True)
class SummaryDeclinedData(SelectedSummaryData, declared_name="summary_declined"):
    status: Literal["declined"]
    reason: str

    def __post_init__(self):
        if not 0 < len(self.reason) <= 256:
            raise ValueError("Invalid summary decline reason")


@dataclass(frozen=True, kw_only=True)
class SummaryUnknownData(SelectedSummaryData, declared_name="summary_unknown"):
    status: Literal["unknown"]
    reason: str | None = None

    def __post_init__(self):
        if self.reason is not None and (
            not 0 < len(self.reason) <= 1024
            or any(ord(c) < 32 or ord(c) == 127 for c in self.reason)
        ):
            raise ValueError("Invalid selected summary failure detail")


@dataclass(frozen=True, kw_only=True)
class SummarySummarizedData(SelectedSummaryData, declared_name="summary_summarized"):
    status: Literal["summarized"]
    witness: NativeWitness
    selected: SelectedModel
    settings: PiCompactionSettings
    result: SummaryResult


@dataclass(frozen=True, kw_only=True)
class SelectedProbeData(PiResponseData):
    strict_fields = True
    version: Literal[1]

    @classmethod
    def wire_member(cls, value):
        return cls.decode("probe_" + str(value.get("status")))


@dataclass(frozen=True, kw_only=True)
class ProbeReadyData(SelectedProbeData, declared_name="probe_ready"):
    status: Literal["ready"]
    route_status: Literal["UNVERIFIED_NO_AUTH_RESOLUTION"] = field(
        metadata={"wire_name": "routeStatus"}
    )
    witness: NativeWitness
    selected: SelectedModel
    settings: PiCompactionSettings


@dataclass(frozen=True, kw_only=True)
class ProbeDeclinedData(SelectedProbeData, declared_name="probe_declined"):
    status: Literal["declined"]
    reason: Literal[
        "busy",
        "queue_nonempty",
        "compacting",
        "source_mismatch",
        "model_mismatch",
        "settings_mismatch",
        "split_turn",
        "unsupported",
    ]


@dataclass(frozen=True)
class CompactionSettingsData(PiResponseData):
    from .owner_compaction_settings import PiCompactionDecision

    strict_fields = True
    version: Literal[1]
    session_id: str = field(metadata={"wire_name": "sessionId"})
    session_file: str = field(metadata={"wire_name": "sessionFile"})
    selected: SelectedModel
    context_tokens: int = field(metadata={"wire_name": "contextTokens"})
    decision: PiCompactionDecision
