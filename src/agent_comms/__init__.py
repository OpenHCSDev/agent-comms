"""OpenHCS agent communications — declaration-owned orchestration."""

__version__ = "0.1.0"

from .exporting import (
    WireExportBoundary,
    WireExportFormat,
    WireExportLimit,
    WireExportProvenance,
    WireExportScope,
    WireTranscriptExporter,
)
from .historical_views import (
    HistoricalMessage,
    HistoricalThread,
    HistorySource,
)
from .page_traversal import HistoryCursor
from .importing import ImportLimits, ThreadImported, ImportSnapshot
from .mentions import MentionQuery
from .read_basis import Conversation, DisplayedConversation, HistoricalDisplay
from .read_ledger import ReadLedger
from .registration import Registration

# CLI defaults ask the declared backend family, so register it before tools.
from . import pi_native_backend, pi_durable_backend
from .agent_backend import AgentBackend
from .tools import context_tool_catalog, invoke_context_tool, invoke_tool, tool_catalog

AgentBackend.seal()

__all__ = [
    "ReadLedger",
    "DisplayedConversation",
    "Conversation",
    "MentionQuery",
    "ImportLimits",
    "ThreadImported",
    "ImportSnapshot",
    "WireExportBoundary",
    "WireExportFormat",
    "WireExportLimit",
    "WireExportProvenance",
    "WireExportScope",
    "WireTranscriptExporter",
    "Registration",
    "HistoryCursor",
    "HistorySource",
    "HistoricalMessage",
    "HistoricalDisplay",
    "HistoricalThread",
    "tool_catalog",
    "context_tool_catalog",
    "invoke_tool",
    "invoke_context_tool",
]
