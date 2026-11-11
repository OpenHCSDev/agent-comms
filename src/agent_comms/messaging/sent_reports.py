"""Publication answers captured on their source before crossing a process boundary."""

from abc import ABC, abstractmethod
from collections.abc import Iterable
from dataclasses import dataclass, field

from agent_comms.messaging.sent_message import PublishedSentMessage
from agent_comms.transcript.transcript_events import SentTranscript


@dataclass(frozen=True)
class SentReportResolution:
    report: SentTranscript
    answer: SentTranscript | PublishedSentMessage


class ResolvesSentReports(ABC):
    """A Send report is qualified against one owner's publication evidence."""

    __slots__ = ()

    @abstractmethod
    def resolve_sent(
        self, messages: tuple[SentTranscript, ...],
    ) -> tuple[SentTranscript | PublishedSentMessage, ...]: ...

    def resolve_sent_messages(
        self, reports: Iterable[SentTranscript],
    ) -> dict[SentTranscript, SentTranscript | PublishedSentMessage]:
        reports = tuple(reports)
        return dict(zip(reports, self.resolve_sent(reports), strict=True))

    def capture_sent_reports(self, reports: Iterable[SentTranscript]) -> tuple[SentReportResolution, ...]:
        return tuple(SentReportResolution(report, answer)
                     for report, answer in self.resolve_sent_messages(reports).items())


@dataclass(frozen=True)
class CapturedSentReports(ResolvesSentReports):
    """Immutable source answers; rendering cannot reopen the source's filesystem."""

    sent_resolutions: tuple[SentReportResolution, ...] = field(default=(), kw_only=True)

    def resolve_sent(
        self, messages: tuple[SentTranscript, ...],
    ) -> tuple[SentTranscript | PublishedSentMessage, ...]:
        answers = {resolution.report: resolution.answer for resolution in self.sent_resolutions}
        return tuple(answers[report] for report in messages)
