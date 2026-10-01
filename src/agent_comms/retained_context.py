"""Turn contributors derived from one certified authored-source read."""

from dataclasses import dataclass

from .messages import Message
from .retained_task_facts import RetainedTaskFacts
from .turn_context import ContextSegment, OwnerProvenance, WireProvenance


@dataclass(frozen=True, kw_only=True)
class RetainedSegment(ContextSegment):
    retained: RetainedTaskFacts

    @classmethod
    def capture(cls, retained: RetainedTaskFacts, source: OwnerProvenance) -> "RetainedSegment":
        sources = dict.fromkeys(source.reference for fact in retained.facts
                                for source in fact.wire_sources())
        return cls(provenance=(source, *(WireProvenance(ref) for ref in sources)),
                   retained=retained)

    def text(self) -> str:
        return self.retained.text

    def original_text_source(self, declaration: Message) -> Message:
        return self.retained.original_text_source(declaration)
