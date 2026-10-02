"""Exact, read-only projections of original task authorities at a source cut.

These values have no update or persistence lifecycle. CompactionSource owns the
captured read; the wire, registry and input document remain the only authorities.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from abc import abstractmethod
from typing import TYPE_CHECKING, Any, ClassVar

from .declared_family import DeclaredFamily
from .errors import RelationViolationError
from .field_codec import FieldCodec
from .goals import Goal
from .input_attempt import StoredInput
from .messages import Message
from .message_reference import MessageReference
from .thread_identity import ThreadRole
from .native_file_artifact import NativeFileArtifact
from .turn_context import JournalProvenance

if TYPE_CHECKING:
    from .input_disposition import InputDocument
    from .input_origin import InputProvenance
    from .registry_document import RegistrySnapshot
    from .threads import Thread


class ExactTaskFact(DeclaredFamily, affix="TaskFact"):
    """Membership belongs to each determining source, not the packing policy."""

    def authored_sources(self) -> tuple[Message, ...]:
        return ()

    def wire_sources(self) -> tuple[Message, ...]:
        return ()

    def original_sources(self):
        return tuple((source.reference, source) for source in self.wire_sources())

    def input_sources(self) -> tuple[StoredInput, ...]:
        return ()

    def for_tasks(self, current: frozenset[MessageReference]) -> ExactTaskFact:
        return self

    def for_owner(self, owner: Thread, registry: RegistrySnapshot) -> ExactTaskFact:
        return self


@dataclass(frozen=True)
class UserSourceTaskFact(ExactTaskFact):
    source: Message

    def wire_sources(self):
        return (self.source,)

    def __post_init__(self) -> None:
        if self.source.sender_role is not ThreadRole.USER:
            raise RelationViolationError("A peer message is not an original user source")


@dataclass(frozen=True)
class AuthoredTaskFact(ExactTaskFact):
    """Captured projection only; its original declaration owns applicability."""
    source: Message

    def authored_sources(self):
        return (self.source,)

    def wire_sources(self):
        return (self.source,)

    @abstractmethod
    def current_fact(self): ...

    @abstractmethod
    def historical_fact(self): ...

    def for_tasks(self, current):
        return (self.current_fact() if self.source.reference in current
                else self.historical_fact())


@dataclass(frozen=True)
class DecisionTaskFact(AuthoredTaskFact, declared_name="historical_decision"):
    def __post_init__(self):
        self.source.require_decision()

    def current_fact(self):
        return CurrentDecisionTaskFact(self.source)

    def historical_fact(self):
        return DecisionTaskFact(self.source)


@dataclass(frozen=True)
class CurrentDecisionTaskFact(DecisionTaskFact, declared_name="current_decision"):
    """An original choice selected by the captured registry and wire lineage."""


@dataclass(frozen=True)
class ConstraintTaskFact(AuthoredTaskFact, declared_name="historical_constraint"):
    def __post_init__(self):
        self.source.task.require_constraint()

    def current_fact(self):
        return CurrentConstraintTaskFact(self.source)

    def historical_fact(self):
        return ConstraintTaskFact(self.source)


@dataclass(frozen=True)
class CurrentConstraintTaskFact(ConstraintTaskFact, declared_name="current_constraint"):
    """Original wording and captured applicability, never a rewritten restriction."""


@dataclass(frozen=True)
class SubtaskTaskFact(AuthoredTaskFact):
    """Keep the original observation; its declaration derives applicability."""
    def __post_init__(self):
        self.source.task.require_subtask()

    def current_fact(self):
        return self

    def historical_fact(self):
        return self


@dataclass(frozen=True)
class UserTaskCorrectionFact(UserSourceTaskFact, AuthoredTaskFact,
                             declared_name="historical_user_correction"):
    def __post_init__(self):
        super().__post_init__()
        self.source.task.require_user_supersession()

    def current_fact(self):
        return CurrentUserTaskCorrectionFact(self.source)

    def historical_fact(self):
        return UserTaskCorrectionFact(self.source)


@dataclass(frozen=True)
class CurrentUserTaskCorrectionFact(UserTaskCorrectionFact,
                                    declared_name="current_user_correction"):
    """The exact human correction selected through its original task scope."""


@dataclass(frozen=True)
class HumanConstraintTaskFact(UserSourceTaskFact, AuthoredTaskFact,
                              declared_name="historical_human_constraint"):
    def __post_init__(self):
        super().__post_init__()
        self.source.task.require_human_constraint()

    def current_fact(self):
        return CurrentHumanConstraintTaskFact(self.source)

    def historical_fact(self):
        return HumanConstraintTaskFact(self.source)


@dataclass(frozen=True)
class CurrentHumanConstraintTaskFact(HumanConstraintTaskFact,
                                     declared_name="current_human_constraint"):
    pass


@dataclass(frozen=True)
class ClaimTaskFact(ExactTaskFact):
    source: Message

    def __post_init__(self) -> None:
        self.source.require_claim_transition()

    def wire_sources(self):
        return (self.source,)


@dataclass(frozen=True)
class GoalTaskFact(ExactTaskFact):
    source: Goal


@dataclass(frozen=True)
class InputTaskFact(ExactTaskFact):
    source: StoredInput

    def input_sources(self) -> tuple[StoredInput, ...]:
        return (self.source,)

    def original_sources(self):
        return tuple((source.context_provenance(), source) for source in self.input_sources())


@dataclass(frozen=True)
class NativeArtifactTaskFact(ExactTaskFact):
    """Exact original result evidence with its existing journal pair coordinates."""

    source: JournalProvenance
    artifact: NativeFileArtifact

    def __post_init__(self):
        if len(self.source.entries) != 2 or len(set(self.source.entries)) != 2:
            raise ValueError("Retained file operation requires distinct original request/result entries")


@dataclass(frozen=True)
class HumanInputTaskFact(InputTaskFact, declared_name="historical_human_input"):
    """Exact human input with recorded scope; no inferred prose constraint kind."""

    def __post_init__(self):
        self.source.origin.require_human()

    def for_owner(self, owner, registry):
        if self.source.origin.require_human().applies(owner, registry):
            return CurrentHumanInputTaskFact(self.source)
        return HumanInputTaskFact(self.source)


@dataclass(frozen=True)
class CurrentHumanInputTaskFact(HumanInputTaskFact, declared_name="current_human_input"):
    pass


@dataclass(frozen=True)
class RetainedTaskFacts:
    """One frozen source projection; no inferred prose facts or replay authority."""

    facts: tuple[ExactTaskFact, ...]

    journal_control_bytes: ClassVar[int] = 65536

    def original_input_facts(self, inputs: InputDocument) -> tuple[ExactTaskFact, ...]:
        """Resolve pins through their original declarations and input owner.

        The lookup is local to this read. A pin cannot supply replacement input
        text, infer an owner or acquire an input's execution disposition.
        """
        originals = {original.key: original for fact in self.facts
                     for message in fact.wire_sources()
                     for original in message.task.original_input_sources(inputs)}
        return tuple(original.origin.retained_fact(original) for original in originals.values())

    def changed_from(self, previous: RetainedTaskFacts) -> dict[str, object]:
        """Compare original captured facts, including multiplicity and disposition.

        These are source differences, not inferred summary correctness or a
        current native capture. The original declarations own serialization.
        """
        from collections import Counter

        before = Counter(self.canonical_journal_bytes(FieldCodec.encode(fact))
                         for fact in previous.facts)
        after = Counter(self.canonical_journal_bytes(FieldCodec.encode(fact))
                        for fact in self.facts)
        return dict(added=[json.loads(raw) for raw in (after - before).elements()],
                    removed=[json.loads(raw) for raw in (before - after).elements()])

    def original_inputs(self, references: tuple[InputProvenance, ...]) -> tuple[StoredInput, ...]:
        """Resolve exact ordered originals in this already captured payload.

        Identity comes from the durable reference; content belongs to the
        original InputTaskFact. Neither a current ledger read nor equal text
        supplies a missing or ambiguous captured original.
        """
        sources: dict[str, list[StoredInput]] = {}
        for fact in self.facts:
            for source in fact.input_sources():
                sources.setdefault(source.key, []).append(source)
        originals = []
        for reference in references:
            candidates = tuple(
                source for source in sources.get(reference.key, ())
                if source.matches_original_provenance(reference)
            )
            if len(candidates) != 1:
                raise RelationViolationError("Retained payload lacks a unique original input")
            originals.append(candidates[0])
        return tuple(originals)

    @staticmethod
    def canonical_journal_bytes(record: object) -> bytes:
        return json.dumps(record, sort_keys=True, separators=(",", ":"),
                          allow_nan=False).encode()

    @classmethod
    def frame_journal(
        cls, record: dict[str, Any], *, retained_payload: bytes = b"null"
    ) -> str:
        """Bound journal controls independently of the exact retained payload.

        The containing owner supplies its exact canonical retained bytes. Native
        CompactionPolicy admits that content against the actual selected model;
        a journal control limit is not another model/context budget. The frozen
        payload remains in the same record, byte-for-byte under the existing
        canonical serialization. No content is shortened or stored elsewhere.

        An outcome supplies no retained payload: its entire observation,
        including an UNKNOWN reason, is control metadata. Read-only source
        projections are measured here without promoting them to fact authority.
        """
        payload = cls.canonical_journal_bytes(record)
        control_bytes = len(payload) - len(retained_payload) + len(b"null")
        if control_bytes > cls.journal_control_bytes:
            raise ValueError("Compaction journal control metadata exceeds bound")
        return payload.decode()

    def original_text_source(self, message: Message) -> Message | StoredInput:
        originals = {reference: source for fact in self.facts
                     for reference, source in fact.original_sources()}
        if originals.get(message.reference) != message:
            raise RelationViolationError("Authored source is outside this captured read")
        return message.task.original_text_source(message, originals)

    def current_authored_lineages(self, owner: Thread, registry: RegistrySnapshot) -> tuple[tuple[Message, Message], ...]:
        """Resolve explicit original-reference lineage, never equal text or time.

        These local maps live only for this read and have no update lifecycle.
        All original records remain present in the immutable source evidence.
        """
        originals: dict[MessageReference, Message] = {}
        effective: dict[MessageReference, tuple[Message, Message]] = {}
        for fact in self.facts:
            for message in fact.authored_sources():
                for root in message.task.current_roots(message, originals, owner, registry):
                    originals[message.reference] = root
                    lineage = root.task.lineage_reference(root)
                    _, previous = effective.get(lineage, (root, root))
                    if message.task.revises_after(previous):
                        effective[lineage] = (root, message)
        return tuple((root, message) for root, message in effective.values()
                     if root.task.require_scoped_task().applies(owner, registry))

    def current_authored_sources(self, owner: Thread, registry: RegistrySnapshot) -> tuple[Message, ...]:
        return tuple(selected for _, message in self.current_authored_lineages(owner, registry)
                     for selected in message.task.selected_sources(message))

    def optional_boundary(self, owner: Thread, registry: RegistrySnapshot) -> tuple[MessageReference, ...]:
        """Latest scoped observation wins, including unfinished or human drop.

        Original wire sequence orders events; correction lineage and owner scope
        come from the same projection used by every retained-source consumer.
        """
        observations = tuple(message for root, message in self.current_authored_lineages(owner, registry)
                             if root.task.observes_subtask)
        if not observations:
            return ()
        latest = max(observations, key=lambda message: message.seq)
        return latest.task.optional_boundary(latest)

    def contains_source(self, reference: MessageReference) -> bool:
        return any(source.reference == reference for fact in self.facts for source in fact.wire_sources())

    def for_owner(self, owner: Thread, registry: RegistrySnapshot) -> RetainedTaskFacts:
        """Classify the same original facts at the existing frozen source cut."""
        selected = self.current_authored_sources(owner, registry)
        for message in selected:
            self.original_text_source(message)
        current = frozenset(message.reference for message in selected)
        return RetainedTaskFacts(tuple(fact.for_tasks(current).for_owner(owner, registry)
                                       for fact in self.facts))

    @property
    def text(self) -> str:
        return (
            "<exact-task-source>\n"
            + json.dumps(FieldCodec.encode(self), ensure_ascii=False, sort_keys=True)
            + "\n</exact-task-source>"
        )

    def require_summary(self, summary: str) -> None:
        if not summary.startswith(self.text + "\n\n"):
            raise RelationViolationError("Native summary omitted its exact task source")
