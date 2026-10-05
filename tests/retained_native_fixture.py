"""Read original retention measurements and reuse the actual native custody owner."""

import asyncio
import hashlib
import json
from collections import Counter
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field, fields, replace
from itertools import chain
from pathlib import Path
from typing import Annotated, Literal

from agent_comms.compaction_identity import SummaryOperationIdentity
from agent_comms.compaction_journal import CompactionJournal
from agent_comms.compaction_records import CompactionOperation, NativeForkCreation, SelectedSummaryAttempt
from agent_comms.field_codec import FieldCodec, FieldRepresentation, PathText
from agent_comms.input_disposition import InputDocument, InputDispositions
from agent_comms.input_attempt import InputAttempt, MissingInput
from agent_comms.native_entries import CompactionEntry, ManagedCompactionEntry, MessageEntry, NativeEntry, NativeEvidenceRead, ThinkingLevelChangeEntry
from agent_comms.native_input_record import NativeInputIdText
from agent_comms.native_pi import NativeContextProof, NativeContextRecord
from agent_comms.native_compaction_request import NativeIntent
from agent_comms.owner_compaction_prepare import NativeWitness
from agent_comms.pi_commands import AgentCommsSummarizeCompaction, PiCommand

from agent_comms.backend import PersistentPiSession
from agent_comms.native_attestation import ObservedAttestation
from agent_comms.native_custody import PiSessionChild
from agent_comms.native_session_reopen import NativeSessionIdentity
from agent_comms.pi_payloads import PiMessage, PiPayload, StateData, ToolResultMessage
from agent_comms.pi_rpc import PiRpcChannel
from agent_comms.pi_rpc import unique_fields
from agent_comms.native_turn_context import NativeContextData, NativeContextManifestData
from agent_comms.registry_document import RegistryDocument
from agent_comms.request_progress import RequestProgress
from agent_comms.turn_lease import TurnLeaseFence
from agent_comms.thread_identity import TurnId
from agent_comms.retained_task_facts import (
    ConstraintTaskFact, DecisionTaskFact, HumanConstraintTaskFact, RetainedTaskFacts,
)
from agent_comms.turn_context import ContextManifest, FileProvenance, JournalProvenance, NativeMessages, NativeProvenance, RecordedContextTurn
from agent_comms.wire_log import WireLog


class SummaryCommandCapture(FieldRepresentation):
    """Expose PiCommand's existing RPC representation to the record codec."""

    @classmethod
    def encode(cls, value):
        return value.to_rpc()

    @classmethod
    def decode(cls, value):
        command = PiCommand.from_wire(value)
        if not isinstance(command, AgentCommsSummarizeCompaction):
            raise ValueError("Summary assembly requires the original selected summary command")
        return command


@dataclass(frozen=True)
class RecordedSummaryAssembly:
    """Original private inspector observation, not a native result or authority.

    Native result/commit records retain packed text. This distinct observation
    retains the original assembly before packing and its actual generated and
    inherited components; it cannot be recovered by stripping a commit.
    """

    strict_fields = True
    request: Annotated[AgentCommsSummarizeCompaction, SummaryCommandCapture]
    summary: str
    generated_parts: tuple[str, ...]
    inherited_summary: str | None

    def require_original(self, attempt, operation):
        expected = replace(self.request, version=1, operation_id=attempt.operation_id,
                           witness=NativeIntent.read(operation).witness,
                           selected=attempt.request.selected,
                           settings=attempt.request.settings,
                           retained_text=attempt.request.retained.text)
        if self.request != expected:
            raise ValueError("Summary assembly belongs to another original selected source")

    def observe(self):
        if self.inherited_summary:
            return {"evaluated": False,
                    "reason": "Original assembly carries a previous packed summary; narrative-only control unavailable"}
        if not self.generated_parts:
            return {"evaluated": False, "reason": "No original generated narrative component"}
        return {"evaluated": True,
                "sha256": hashlib.sha256(self.summary.encode()).hexdigest(),
                "utf8_bytes": len(self.summary.encode()),
                "generated_part_count": len(self.generated_parts),
                "scope": "Original pre-pack assembly; not packed-budget admission, submitted context, HTTP bytes or recall"}


@dataclass(frozen=True)
class RecordedNativeCheckpoint:
    """References to original measured evidence, never a replacement checkpoint."""

    journal: Annotated[Path, PathText]
    reference: SummaryOperationIdentity
    commit_id: str
    # Optional external measurement evidence, not native lifecycle state. A
    # missing original capture cannot be reconstructed from today's registry.
    registry_scope: FileProvenance | None = None
    wire: Annotated[Path | None, PathText] = None
    summary_assembly: FileProvenance | None = None

    def capture_summary_observation(self, directory: Path):
        """Pin the optional original inspector artifact without reconstructing it."""
        path = directory / f"summary-{self.reference.operation_id}.json"
        if not path.is_file():
            return self
        return replace(self, summary_assembly=FileProvenance(
            str(path), hashlib.sha256(path.read_bytes()).hexdigest()))

    @staticmethod
    def read_bytes(reference: FileProvenance):
        """Read unchanged original bytes before their declared boundary decodes."""
        raw = Path(reference.path).read_bytes()
        if hashlib.sha256(raw).hexdigest() != reference.sha256:
            raise ValueError("Recorded measurement artifact changed")
        return raw

    @staticmethod
    def snapshot_observation(source: Path, destination: Path):
        """Seal original appendable observation bytes before pinning a file.

        Native diagnostics and inspector completion can append after a probe.
        A FileProvenance names an immutable captured value, not that writer's
        later contents. Preserve the raw bytes without filtering or recoding.
        """
        raw = source.read_bytes()
        with destination.open('xb') as stream:
            stream.write(raw)
        destination.chmod(0o600)
        return FileProvenance(str(destination), hashlib.sha256(raw).hexdigest())

    @classmethod
    def read_json(cls, reference: FileProvenance):
        return json.loads(cls.read_bytes(reference), object_pairs_hook=unique_fields)

    @classmethod
    def read_json_lines(cls, reference: FileProvenance):
        return tuple(json.loads(line, object_pairs_hook=unique_fields)
                     for line in cls.read_bytes(reference).splitlines())

    @classmethod
    def read_record(cls, reference: FileProvenance, target):
        """Decode a pinned internal record through its existing declaration."""
        return FieldCodec.decode(target, cls.read_json(reference))

    def summary_usage(self, entry):
        # capture() already verifies the original metadata digest, which covers
        # NativeSummaryPayload.usage. Never decode another usage vocabulary.
        if entry.usage is None:
            return {"evaluated": False, "reason": "Original summary usage not retained"}
        return {"evaluated": True, "usage": entry.usage,
                "tokens_before": entry.tokens_before,
                "scope": "Original SDK-normalized counters; no cache savings or paired cost inference"}

    def capture(self, session: NativeSessionIdentity, evidence: NativeEvidenceRead):
        """Borrow original records after the journal/native owners corroborate them."""
        session.require_session(self.reference.session_file)
        evidence.require_path(Path(session.session_file))
        header, entries = evidence.observe()
        session.require_same_session(NativeSessionIdentity(header.id, str(evidence.source.path)))

        def read(db):
            attempt = SelectedSummaryAttempt.one(db, operation_id=self.reference.operation_id)
            if attempt is None:
                raise ValueError("Recorded checkpoint has no original selected request")
            attempt.require_session(session.session_file)
            operation = CompactionOperation.one(db, commit_id=self.commit_id)
            if operation is None:
                raise ValueError("Recorded checkpoint has no original commit")
            # Read-only linkage verifies the original intent/source digest. It
            # does not obtain or consume the commit owner's returned ACK.
            operation.require_summary_link(attempt, admit_original=True)
            original = operation.committed_outcome()
            entry, = (row for row in entries if row.id == original.entry_id)
            if not isinstance(entry, ManagedCompactionEntry):
                raise ValueError("Recorded checkpoint requires an original managed native compaction")
            branch = evidence.branch(entry.id, entries)
            covered = operation.covered_prefix(entry, evidence, branch)
            attempt.request.retained.require_summary(entry.summary)
            assembly = None
            if self.summary_assembly is not None:
                assembly = self.read_record(self.summary_assembly, RecordedSummaryAssembly)
                assembly.require_original(attempt, operation)
            return attempt, entry, covered, assembly

        original = CompactionJournal.observe_readonly(self.journal, read, absent=None)
        if original is None:
            raise ValueError("Recorded checkpoint has no original compaction journal")
        return original

    def _report(self, attempt: SelectedSummaryAttempt, entry: ManagedCompactionEntry,
               covered: frozenset[str], assembly: RecordedSummaryAssembly | None):
        # Membership comes from the original declared fact family. These counts
        # describe the corroborated envelope, not inferred summary prose.
        membership = Counter(fact.declared_name for fact in attempt.request.retained.facts)
        return {
            "reference": FieldCodec.encode(attempt.identity),
            "commit_id": self.commit_id,
            "native_entry_id": entry.id,
            "retained_facts": FieldCodec.encode(attempt.request.retained),
            "selected_model": FieldCodec.encode(attempt.request.selected),
            "settings": FieldCodec.encode(attempt.request.settings),
            "summary_usage": self.summary_usage(entry),
            "scoped_facts": self.scoped_facts(attempt),
            "summary_narrative": assembly.observe() if assembly is not None else {
                "evaluated": False, "reason": "Original pre-pack summary assembly not captured"},
            "revision_mass": {
                "evaluated": False,
                "reason": "An original scope and authorized correction evidence are required, not content differences",
            },
            "canonical_availability": {
                "scope": "exact original selected-source envelope in its corroborated native commit",
                "evaluated": bool(attempt.request.retained.facts),
                "reason": "An empty retained envelope has no eligible fact denominator"
                          if not attempt.request.retained.facts else "Original envelope verified against committed payload",
                "source_digest": attempt.request.retained.source_digest.value,
                "required": len(attempt.request.retained.facts),
                "available": len(attempt.request.retained.facts),
                "by_fact": {kind: {"required": count, "available": count}
                            for kind, count in sorted(membership.items())},
                "covered_native_entries": len(covered),
            },
        }

    def observe(self, session: NativeSessionIdentity, evidence: NativeEvidenceRead):
        return self._report(*self.capture(session, evidence))

    @contextmanager
    def original_source(self):
        """Borrow the unchanged native owner for standalone recorded inspection."""
        with NativeEntry.open_evidence(Path(self.reference.session_file)) as evidence:
            header, _ = evidence.observe()
            yield NativeSessionIdentity(header.id, str(evidence.source.path)), evidence

    def condition_source(self, session: NativeSessionIdentity, evidence: NativeEvidenceRead):
        """Private construction input from a corroborated original checkpoint.

        This exports original narrative bytes only when that original capture
        exists and is eligible. It is not an input grant or a submitted control.
        SDK construction owns the current kept range and native conversion.
        """
        _, entry, _, assembly = self.capture(session, evidence)
        return self._condition_source(session,entry,assembly)

    def _condition_source(self,session,entry,assembly):
        if assembly is None:
            return {"evaluated": False,
                    "reason": "Original pre-pack summary assembly not captured"}
        observed = assembly.observe()
        if not observed["evaluated"]:
            return observed
        return {**observed, "session": FieldCodec.encode(session),
                "checkpoint_session": FieldCodec.encode(session),
                "native_entry_id": entry.id, "summary": assembly.summary,
                "source": FieldCodec.encode(self.summary_assembly)}

    def _fork_capture(self,journal:Path,child:NativeEvidenceRead,source:NativeEvidenceRead):
        """Original creation owns inheritance for both construction and scoring."""
        session_file=child.source.path
        creation = CompactionJournal.observe_readonly(journal, lambda db:
            NativeForkCreation.one(db, session_file=str(session_file)), absent=None)
        if creation is None:
            raise ValueError("Bounded child requires its original recorded SDK fork")
        header,_=source.observe()
        original=NativeSessionIdentity(header.id,str(source.source.path))
        creation.source.require_same_session(original)
        captured=self.capture(original,source)
        header,entries=child.observe()
        selected=NativeSessionIdentity(header.id,str(session_file))
        creation.require_same_session(selected)
        inherited=creation.covered_prefix(child,entries)
        entry=captured[1]
        if entry.id not in inherited:
            raise ValueError("Original checkpoint is outside the SDK fork prefix")
        copied,_=child.entry_index(entries)[entry.id]
        if copied!=entry:
            raise ValueError("Original checkpoint differs from its inherited SDK entry")
        return creation,original,captured

    def fork_condition_source(self,journal:Path,session_file:Path):
        """Bind captured narrative through the recorded SDK creation, not input authority."""
        with NativeEntry.open_evidence(session_file) as child, self.original_source() as (_,source):
            return self.fork_condition_acquired(journal,child,source)

    def fork_condition_acquired(self,journal,child,source):
        """Use the caller's original readers with the same fork/source proof."""
        creation,original,(_,entry,_,assembly)=self._fork_capture(journal,child,source)
        constructed=self._condition_source(original,entry,assembly)
        if not constructed['evaluated']:
            return constructed
        return {**constructed,'session':FieldCodec.encode(NativeSessionIdentity(
            creation.session_id,creation.session_file)),
                'fork_creation':FieldCodec.encode(creation)}

    def fork_request_narrative(self,journal,child,source,texts):
        """Corroborate the original fork narrative in acquired SDK bytes."""
        constructed=self.fork_condition_acquired(journal,child,source)
        if constructed['evaluated']:
            narrative=json.dumps(constructed['summary'],ensure_ascii=False)[1:-1]
            if not any(narrative in text for text in texts):
                raise ValueError('Captured SDK request does not contain the original bounded narrative')
        return constructed

    def capture_for_probe(self,session,evidence,fork_journal,source,input_entry):
        """Corroborate the latest cut on this original input's acquired branch.

        A completed cut may already contain subsequent messages when forked.
        Those messages preserve the cut's applicability; another compaction or
        another branch does not. Fork inheritance still needs its original
        creation and exact parent/child source evidence.
        """
        if self.reference.session_file==session.session_file:
            captured=self.capture(session,evidence)
        else:
            if fork_journal is None:
                raise ValueError('Inherited checkpoint probe requires its original fork journal')
            creation,_,captured=self._fork_capture(fork_journal,evidence,source)
            creation.require_same_session(session)
        branch=evidence.branch(input_entry.require_entry_id(),evidence.entries)
        cuts=tuple(entry for entry in branch if isinstance(entry,CompactionEntry))
        if not cuts or cuts[-1]!=captured[1]:
            raise ValueError('Recorded checkpoint is not the latest compaction on its original input branch')
        return captured

    def inspect(self, previous: RecordedNativeCheckpoint | None = None):
        """Read a checkpoint or original ancestor interval without new input.

        The optional previous reference is external evaluation input. It neither
        selects a live source nor carries native lifecycle/admission state.
        """
        with self.original_source() as (session, evidence):
            current = self.capture(session, evidence)
            report = self._report(*current)
            if previous is not None:
                report.update(self.compare_acquired(previous,
                    previous.capture(session, evidence), current, evidence))
            return report

    def compare_acquired(self, previous, before, after, evidence):
        """Compare the two already corroborated original cuts once.

        This owns source ancestry and the original retained-fact difference.
        The frozen scenario separately owns which interval was requested; an
        observed wider interval cannot fill a missing adjacent-round measure.
        """
        prior_attempt, prior_entry, _, _ = before
        current_attempt, current_entry, _, _ = after
        branch = evidence.branch(current_entry.id, evidence.entries)
        if prior_entry.id == current_entry.id or prior_entry not in branch:
            raise ValueError("Checkpoint comparison requires distinct original ancestor cuts")
        return {
            "revision_mass": self.revision_from(previous, prior_attempt, current_attempt),
            "source_changes": {
                "previous": FieldCodec.encode(prior_attempt.identity),
                "current": FieldCodec.encode(current_attempt.identity),
                **current_attempt.request.retained.changed_from(prior_attempt.request.retained),
            },
        }

    def authored_scope(self, attempt):
        """Acquire the original captured scope and certify its authored rows.

        Revision and action measurements borrow the same owner relation; this
        neither selects today's scope nor treats an omitted row as nonexistent.
        """
        if self.registry_scope is None or self.wire is None:
            return None
        snapshot = self.read_record(self.registry_scope, RegistryDocument).snapshot()
        owner = snapshot.require_active(attempt.request.source.incarnation.name)
        if attempt.request.source.incarnation.resolved(snapshot) != owner.incarnation:
            raise ValueError("Authored evidence belongs to another original owner")
        messages = tuple(message for fact in attempt.request.retained.facts
                         for message in fact.wire_sources())
        with WireLog(self.wire).certified_read() as source:
            deliveries = source.references(tuple(message.reference for message in messages))
            for message, delivery in zip(messages, deliveries, strict=True):
                if delivery.message != message:
                    raise ValueError("Retained wire source differs from its original publication")
        return snapshot, owner

    @staticmethod
    def authored_lineages(retained, owner, registry, families):
        """Select the measured family from the original lineage projection."""
        roots = {message.reference for fact in retained.facts
                 if isinstance(fact, families) for message in fact.authored_sources()}
        return tuple((root, message) for root, message in
                     retained.current_authored_lineages(owner, registry)
                     if root.reference in roots)

    def revision_from(self, previous, before, after):
        """Measure captured authored lineages using the original scope owner.

        The journal cut authenticates captured facts; the certified wire read
        corroborates their original publications. Existing task declarations
        decide correction lineage and scope. This read grants no update/replay.
        """
        prior_scope = previous.authored_scope(before)
        current_scope = self.authored_scope(after)
        if prior_scope is None or current_scope is None:
            return {"evaluated": False,
                    "reason": "Original scope snapshots and certified wire evidence are required"}
        prior, before_owner = prior_scope
        current, after_owner = current_scope
        if before_owner.incarnation.resolved(current) != after_owner.incarnation:
            raise ValueError("Original revision scopes belong to different owner incarnations")
        original_facts = before.request.retained.facts + after.request.retained.facts
        # Multiplicity belongs to availability. A lineage projection visits each
        # certified original publication once. Fact encoding/classification is
        # not publication identity; the message declaration derives its facts.
        originals = {message.reference: message for fact in original_facts
                     for message in fact.authored_sources()}
        combined = RetainedTaskFacts(tuple(fact
            for message in sorted(originals.values(), key=lambda message: message.seq)
            for fact in message.retained_task_facts()))

        def selected(retained, owner, registry, families):
            return {root.task.lineage_reference(root): tuple(message.task.selected_sources(message))
                    for root, message in self.authored_lineages(retained, owner, registry, families)}

        def measure(families):
            eligible = selected(before.request.retained, before_owner, prior, families)
            expected = selected(combined, after_owner, current, families)
            observed = selected(after.request.retained, after_owner, current, families)
            unauthorized, authorized, ended = [], [], []
            for identity, original in eligible.items():
                desired = expected.get(identity, ())
                actual = observed.get(identity, ())
                if actual != desired:
                    unauthorized.append(FieldCodec.encode(identity))
                elif desired != original:
                    (authorized if desired else ended).append(FieldCodec.encode(identity))
            denominator = len(eligible)
            return {"evaluated": bool(denominator), "eligible": denominator,
                    "unauthorized": len(unauthorized), "identities": unauthorized,
                    "mass": len(unauthorized) / denominator if denominator else None,
                    "authorized_supersessions": authorized, "scope_or_explicit_drop": ended,
                    "additions": [FieldCodec.encode(identity) for identity in observed.keys() - eligible.keys()],
                    "reason": "Original scoped lineages compared" if denominator
                              else "No eligible authored identities"}

        measured = {"constraints": measure((ConstraintTaskFact, HumanConstraintTaskFact)),
                    "decisions": measure(DecisionTaskFact)}
        return {"evaluated": any(group["evaluated"] for group in measured.values()),
                "scope": "Original captured scopes and certified authored task publications",
                **measured}


    def scoped_facts(self, attempt):
        """Read captured configuration and original retained source references.

        The existing task attachment resolves a declaration's original wording;
        a pin's own body is not its USER subject. These references describe the
        retained payload, not a delivered native input or submitted request.
        """
        captured = self.authored_scope(attempt)
        if captured is None:
            return {"evaluated": False, "reason": "Original scope/publications unavailable",
                    "configured_settings": {"evaluated": False},
                    "retained_publications": {"evaluated": False,
                        "reason": "Original scope/publications unavailable"}}
        snapshot, owner = captured
        retained = attempt.request.retained
        declarations = self.authored_lineages(retained, owner, snapshot, DecisionTaskFact)
        originals = {message.reference: message for fact in retained.facts
                     for message in fact.wire_sources()}
        authored = {message.reference: message for fact in retained.facts
                    for message in fact.authored_sources()}
        return {"evaluated": True,
                "scope": "Original captured configuration, retained publications and scoped Decision alternatives",
                "retained_publications": {
                    "evaluated": bool(originals),
                    "references": tuple(originals),
                    "source_digest": retained.source_digest.value,
                    "authored_wordings": tuple({
                        "declaration": message.reference,
                        "wording": message.task.original_wording_context_source(
                            retained.original_text_source(message)),
                    } for message in authored.values()),
                    "reason": "Original retained wire rows corroborated" if originals
                              else "No retained wire publications to measure",
                    "scope": "Exact retained publications and original wording references; "
                             "not native input delivery, request presence, HTTP bytes or recall",
                },
                "configured_settings": {"evaluated": owner.model is not None and owner.thinking_level is not None,
                                        "model": owner.model, "thinking": FieldCodec.encode(owner.thinking_level),
                                        "scope": "Captured registry configuration, not provider-reported request selection"},
                "decisions": tuple({"lineage": root.reference,
                                    "current": message.reference,
                                    "declaration": message.task.require_decision()}
                                   for root, message in declarations)}


@dataclass(frozen=True)
class RecordedConditionInstallation(PiPayload):
    """One original private SDK hook observation, not enrollment or a grant.

    This is a new observation: installed source entering the configured transform.
    NativeWitness owns session/revision meaning; it is not redeclared here.
    """

    strict_fields = True
    stage: Literal['installed-transform-applied']
    input_id: Annotated[str, NativeInputIdText]
    condition: str
    source_witness: NativeWitness
    construction_context_sha256: str
    source_prefix_count: int
    source_prefix_sha256: str
    source_message_count: int
    message_count: int
    agent_messages_sha256: str
    # Optional original private observation. Older records and constructors
    # which borrow live SDK messages do not prove a journal entry selection.
    entry_selection: JournalProvenance | None = field(default=None,
        metadata={"wire_omit_default": True})
    # Original EntryStore.uncompactedMetadata selection at this same witness.
    # Older observations did not retain it; never infer it from an arm label.
    uncompacted_selection: JournalProvenance | None = field(default=None,
        metadata={"wire_omit_default": True})
    # An original constructor resource, not inferred from its condition label.
    narrative_source: FileProvenance | None = field(default=None,
        metadata={"wire_omit_default": True})
    construction_manifest: NativeContextManifestData | None = field(default=None,
        metadata={"wire_omit_default": True})

    def to_wire(self):
        """Retain the nested SDK manifest's original external representation."""
        data = super().to_wire()
        if self.construction_manifest is not None:
            data['construction_manifest'] = self.construction_manifest.to_wire()
        return data

    def require_narrative_source(self, source: FileProvenance):
        if self.narrative_source != source:
            raise ValueError('Recorded SDK installation belongs to another original narrative source')
        return source

    def require_condition(self, selected: str):
        """An observed constructor cannot stand in for a different arm.

        This is the original hook's selection, not complete transformed source
        or provider intervention proof. Absent observations remain absent.
        """
        if self.condition != selected:
            raise ValueError("Declared retention arm differs from its original SDK constructor")
        return self

    def require_original(self, probe, evidence, branch, context):
        if self.input_id != probe.input_id:
            raise ValueError('Installed SDK source belongs to another input')
        probe.session.require_same_session(self.source_witness)
        if not self.source_witness.covers(evidence,self.source_witness.revision):
            raise ValueError('Installed SDK source revision is not covered by original input history')
        prefix=tuple(entry for entry in branch[:next(index for index,entry in enumerate(branch)
                     if entry.id==context.session_entry_id)])
        if self.source_witness.leaf_id not in evidence.entry_index(prefix):
            raise ValueError('Installed SDK source is not an ancestor of the original input')
        if not 0 <= self.source_prefix_count <= self.source_message_count:
            raise ValueError('Installed SDK source counts differ from its observed transform')
        ancestry=evidence.branch(self.source_witness.leaf_id, prefix)
        if self.construction_manifest is not None:
            if self.construction_manifest.request_id is not None:
                raise ValueError('Constructed source observation is a request, not the original prefix')
            available=evidence.entry_index(ancestry)
            for segment in self.construction_manifest.segments:
                self.source_witness.require_same_session(segment.native_identity())
                for source in segment.source_membership():
                    for identity in source.native_identities():
                        self.source_witness.require_same_session(identity)
                    if any(entry not in available for entry in source.journal_entries(self.source_witness)):
                        raise ValueError('Constructed source coordinates are outside the original installation ancestry')
        return ancestry

    @staticmethod
    def message_partition(segments):
        """Use the SDK's complete message parts, never annotation children."""
        parts=[]
        coordinates=[]
        for index,segment in enumerate(segments):
            if not issubclass(segment.kind,NativeMessages):
                continue
            messages=segment.recorded_parts()
            if not messages:
                return {'evaluated':False,'reason':'Original complete SDK message partition not captured'}
            parts.extend(messages)
            coordinates.extend((index,position) for position in range(len(messages)))
        return {'evaluated':True,'parts':tuple(parts),'coordinates':tuple(coordinates)}

    def constructed_prefix(self, request_partition, binding):
        """Compare the entire acquired construction against this SDK request.

        Grouping may change when the fresh input is appended. Individual SDK
        parts preserve the original order and exact kind/byte digest/size.
        Extension changes are observations, not a reason to reject native input.
        """
        if self.construction_manifest is None:
            return {'evaluated':False,'reason':'Original constructor segment manifest not captured'}
        construction=self.message_partition(self.construction_manifest.segments)
        if not construction['evaluated']:
            return construction
        if not request_partition['evaluated']:
            return request_partition
        expected=construction['parts']
        actual=request_partition['parts'][:len(expected)]
        preserved=len(actual)==len(expected) and all(
            (a.kind,a.sha256,a.utf8_bytes)==(b.kind,b.sha256,b.utf8_bytes)
            for a,b in zip(expected,actual))
        return {'evaluated':binding['evaluated'],'preserved':preserved,
            'constructed_messages':len(expected),'request_messages':len(request_partition['parts']),
            'construction_coordinates':construction['coordinates'],
            'request_coordinates':request_partition['coordinates'][:len(expected)],
            'original_parts':expected,'request_parts':actual,'message_binding':binding,
            'scope':'Complete ordered constructor message partition compared with the captured SDK request prefix; '
                    'requires original transform/converter/request binding; not HTTP bytes, capacity or a matched study'}

    @staticmethod
    def require_selection(source, identity, ancestry):
        """Both original SDK selections borrow the same acquired ancestry."""
        selected = source.journal_entries(identity)
        available = tuple(entry.require_entry_id() for entry in ancestry)
        selected_set = set(selected)
        if len(selected_set) != len(selected) or tuple(
                entry for entry in available if entry in selected_set) != selected:
            raise ValueError('SDK selected entries are not an ordered subset of the original construction ancestry')
        return selected

    def source_selection(self, identity, ancestry):
        """Compare SDK-owned selections, never a label or message count."""
        full = (self.require_selection(self.uncompacted_selection, identity, ancestry)
                if self.uncompacted_selection is not None else None)
        selected = (self.require_selection(self.entry_selection, identity, ancestry)
                    if self.entry_selection is not None else None)
        complete = {'evaluated': False,
            'reason': 'Original selected and uncompacted SDK entry references required'}
        if full is not None and selected is not None:
            complete = {'evaluated': True, 'selected': selected == full,
                'source': self.uncompacted_selection,
                'scope': 'Exact ordered SDK uncompacted entry selection at the original construction witness; '
                         'not transformed bytes, admission or provider capacity'}
        if selected is None:
            return {'evaluated': False, 'uncompacted_selection': complete,
                'reason': 'Original SDK journal entry selection was not captured'}
        selected_set = set(selected)
        messages = tuple(entry.require_entry_id() for entry in ancestry if entry.is_message)
        return {'evaluated': True, 'source': self.entry_selection,
            'uncompacted_selection': complete,
            'original_message_entries': messages,
            'selected_message_entries': tuple(entry for entry in messages if entry in selected_set),
            'unselected_message_entries': tuple(entry for entry in messages if entry not in selected_set),
            'scope': 'Original SDK entry-based construction before conversion; not complete transformed content, '
                     'provider token capacity, HTTP bytes or a registered intervention'}


@dataclass(frozen=True)
class RecordedNativeProbe:
    """One original probe and answer; tool-assisted answers are labelled separately."""

    session: NativeSessionIdentity
    input_id: Annotated[str, NativeInputIdText]
    answer_entry_id: str
    # Full-context controls have no compaction checkpoint. This is an explicit
    # external measurement field, not a nullable native lifecycle state.
    checkpoint: RecordedNativeCheckpoint | None = None
    sdk_context: FileProvenance | None = None
    context_manifest: FileProvenance | None = None
    sdk_segment_bytes: FileProvenance | None = None
    submitted_inputs: FileProvenance | None = None
    # Controls without a summary still need the SDK's original fork record.
    fork_journal: Annotated[Path | None, PathText] = None
    # Original diagnostic publication, not a reconstructed request or budget.
    request_observations: FileProvenance | None = None
    condition_observation: FileProvenance | None = None
    # The original emitted SDK event, before its public-text wire projection.
    # Historical captures did not retain it; do not synthesize its values.
    sdk_observation: FileProvenance | None = None

    @classmethod
    def capture_input(cls,service,owner,session,row,contexts,output,checkpoint=None,condition_observation=None):
        """Acquire this completed input's original source publications once."""
        from agent_comms.diagnostics import request_observation_path

        def pin(name,value):
            path=output/f'probe-{row.native_id}-{name}.private.json'
            with path.open('x') as stream:
                json.dump(FieldCodec.encode(value),stream,ensure_ascii=False)
                stream.write('\n')
            path.chmod(0o600)
            return FileProvenance(str(path),hashlib.sha256(path.read_bytes()).hexdigest())

        with NativeEntry.open_evidence(Path(session.session_file)) as evidence:
            context=NativeContextProof.read_evidence(Path(session.session_file),row.native_id,evidence=evidence)
            answer,_=cls.answer_for_input(evidence,context)
            provenance=NativeProvenance(session,context.request_generation,context.llm_context_digest)
            manifest,=(manifest for manifest in service.bus.log.context_manifests(owner.name,service.registry)
                       if manifest.segments and all(provenance in segment.provenance for segment in manifest.segments))
        def original(path):
            return FileProvenance(str(path),hashlib.sha256(path.read_bytes()).hexdigest())

        observed=request_observation_path(service.root,row.turn_id)
        sdk_observed=contexts/f'observation-{context.request_generation}-{context.llm_context_digest}.json'
        return cls(session,row.native_id,answer.id,checkpoint,
            original(contexts/f'context-{context.llm_context_digest}.json'),
            pin('manifest',manifest),original(contexts/f'segments-{context.llm_context_digest}.json'),
            pin('inputs',InputDispositions(service.root/InputDispositions.filename).read()),
            fork_journal=service.root/'compaction-commits.sqlite3',
            request_observations=RecordedNativeCheckpoint.snapshot_observation(observed,
                output/f'probe-{row.native_id}-requests.private.jsonl') if observed.is_file() else None,
            condition_observation=RecordedNativeCheckpoint.snapshot_observation(condition_observation,
                output/f'probe-{row.native_id}-condition.private.jsonl') if condition_observation is not None else None,
            sdk_observation=original(sdk_observed) if sdk_observed.is_file() else None)

    def condition_records(self):
        """Acquire this original observation resource once for both questions."""
        return (RecordedNativeCheckpoint.read_json_lines(self.condition_observation)
                if self.condition_observation is not None else ())

    def applied_condition(self,evidence,parent,texts,serialized,manifest,records):
        """Join this input's SDK hook to the corroborated narrative and actual bytes."""
        if self.condition_observation is None:
            return {'evaluated':False,'reason':'Original condition application not captured'}
        if self.checkpoint is None or self.fork_journal is None or not serialized['evaluated']:
            return {'evaluated':False,'reason':'Original checkpoint, fork and SDK bytes required'}
        applications=tuple(row for row in records if row.get('stage')=='bounded-transform-applied'
                           and row['input_id']==self.input_id)
        if not applications:
            return {'evaluated':False,'reason':'Original bounded-source hook observation unavailable'}
        source=self.checkpoint.fork_request_narrative(self.fork_journal,evidence,parent,texts)
        if not source['evaluated']:
            return source
        for applied in applications:
            if (applied['session']!=source['session'] or
                    applied['checkpoint_session']!=source['checkpoint_session'] or
                    applied['native_entry_id']!=source['native_entry_id'] or
                    applied['narrative_source']!=source['source']):
                raise ValueError('Recorded SDK condition belongs to another original source')
        if not any(row.get('stage')=='bounded-transform-restored' and row['input_id']==self.input_id for row in records):
            raise ValueError('Original SDK condition hook has not retired')
        binding=self.condition_message_binding(records,applications,serialized,manifest)
        return {'evaluated':binding['evaluated'], 'observation':self.condition_observation,
            'transform':{'evaluated':True,'narrative_source':source['source'],
                'checkpoint_session':source['checkpoint_session'],'session':source['session'],
                'native_entry_id':source['native_entry_id'],
                'scope':'Original SDK transform, narrative presence and retired hook; not complete request binding'},
            'message_binding':binding,
            'scope':'Original bounded transform joined to this sealed SDK request; not final HTTP bytes, '
                    'complete-history capacity, declared comparison arm or comparative recall'}

    def installed_condition(self,evidence,branch,context,serialized,manifest,records,*,parent,texts):
        """Join an observed installed source to the same original SDK request."""
        originals=tuple(RecordedConditionInstallation.from_wire(row) for row in records
            if row.get('stage')=='installed-transform-applied' and row['input_id']==self.input_id)
        unavailable = {'evaluated': False, 'reason': 'Original SDK journal entry selection unavailable'}
        narrative = {'evaluated': False, 'reason': 'Original installed narrative source and matching SDK bytes unavailable'}
        prefix = {'evaluated':False,'reason':'Original constructor and bound SDK request partition unavailable'}
        if not originals:
            return {'evaluated':False,'installations':(), 'entry_selection':unavailable, 'narrative_source':narrative,
                'constructed_prefix':prefix,
                'reason':'Original installed-source hook observation unavailable'}
        selections = tuple(original.source_selection(self.session,
            original.require_original(self,evidence,branch,context)) for original in originals)
        if not any(row.get('stage')=='installed-transform-restored'
                   and row['input_id']==self.input_id for row in records):
            raise ValueError('Original installed-source hook has not retired')
        selection = {'evaluated':all(value['evaluated'] for value in selections),
            'observations':selections,
            'scope':'SDK selected source before this original input; source selection is distinct '
                    'from transform/request admission and provider capacity'}
        observation = {'observation': self.condition_observation,
                       'installations': originals, 'entry_selection': selection, 'narrative_source':narrative,
                       'constructed_prefix':prefix}
        if not serialized['evaluated'] or manifest is None:
            return dict(observation, evaluated=False,
                        reason='Original matching SDK request bytes unavailable')
        applications=tuple(FieldCodec.encode(original) for original in originals)
        binding=self.condition_message_binding(records,applications,serialized,manifest)
        partition=RecordedConditionInstallation.message_partition(manifest.segments)
        prefixes=tuple(original.constructed_prefix(partition,binding) for original in originals)
        prefix={'evaluated':all(value['evaluated'] for value in prefixes),'observations':prefixes}
        if prefix['evaluated']:
            prefix['preserved']=all(value['preserved'] for value in prefixes)
        referenced=tuple(original for original in originals if original.narrative_source is not None)
        if (self.checkpoint is not None and self.fork_journal is not None and
                referenced):
            source=self.checkpoint.fork_request_narrative(self.fork_journal,evidence,parent,texts)
            if source['evaluated']:
                reference=FieldCodec.decode(FileProvenance,source['source'])
                for original in referenced:
                    original.require_narrative_source(reference)
                narrative={'evaluated':binding['evaluated'] and len(referenced)==len(originals), 'source':reference,
                    'checkpoint_session':source['checkpoint_session'], 'session':source['session'],
                    'native_entry_id':source['native_entry_id'],
                    'message_binding':binding,
                    'scope':'Original installed narrative resource, fork ancestry and captured SDK presence; '
                            'request binding requires the original complete converter observation; '
                            'not narrative-only input, HTTP bytes, capacity or a matched intervention'}
            else:
                narrative=source
        observation['narrative_source']=narrative
        observation['constructed_prefix']=prefix
        return dict(observation, evaluated=binding['evaluated'], message_binding=binding,
            scope='Original installed source entered configured transform and converter/request; '
                  'constructor tag is observed metadata, not matched-arm, HTTP or capacity proof')

    def condition_message_binding(self,records,applications,serialized,manifest):
        """Bind one actual converter result to the sealed SDK request bytes.

        The inspector observes the original agent-loop frame after conversion;
        no converter is called again. Only that request's transformation joins
        its complete message sequence. Historical missing observations cannot
        be reconstructed from narrative presence or today's SDK converter.
        """
        if manifest.request_id is None:
            return {'evaluated':False,'reason':'Original request ID not captured'}
        conversions=tuple(row for row in records
            if row.get('stage')=='bounded-conversion-observed'
            and row['request_id']==manifest.request_id)
        if not conversions:
            return {'evaluated':False,'reason':'Original SDK conversion not captured'}
        conversion,=conversions
        if (conversion['session_id']!=self.session.session_id or
                conversion['input_id']!=self.input_id):
            raise ValueError('Original SDK conversion belongs to another session/input')
        applied=tuple(row for row in applications
            if row.get('agent_messages_sha256')==conversion['agent_messages_sha256'])
        if not applied:
            raise ValueError('Original SDK conversion differs from the applied condition')
        if conversion['provider_messages_sha256']!=serialized['provider_messages_sha256']:
            raise ValueError('Original SDK conversion differs from the captured request messages')
        return {'evaluated':True,'request_id':manifest.request_id,
            'agent_messages_sha256':conversion['agent_messages_sha256'],
            'provider_messages_sha256':conversion['provider_messages_sha256'],
            'scope':'Complete original SDK transformed/converter message sequence; not payload hooks, HTTP bytes, full-history capacity or comparative recall'}

    def submitted_prompt(self, user, manifest):
        """Bind an original submitted source to its exact recorded native write.

        A direct-native control measures its native user text. An ACP capture
        supplies the original InputDocument, whose STARTED member owns both
        submitted and rendered text. The sealed manifest supplies the expected
        turn; original diagnostic leases supply admission separately. Missing
        evidence cannot be replaced by the row's own answer. This is
        measurement, never lease authority.
        """
        if self.submitted_inputs is None:
            return user.message.text, {"scope": "original native user text"}, MissingInput()
        document = RecordedNativeCheckpoint.read_record(self.submitted_inputs, InputDocument)
        row, = (row for row in document.rows.values()
                if row.has_started and row.native_id == self.input_id)
        turn_id = row.turn_id if manifest is None else manifest.turn.require_recorded().identity.value
        if not row.matches_native(turn_id=turn_id, native_id=self.input_id, text=user.message.text):
            raise ValueError("Original submitted input differs from the recorded native write")
        scope = ("original STARTED InputDocument source, exact sent text and sealed recorded turn"
                 if manifest is not None else
                 "original STARTED InputDocument source and exact sent text; recorded turn unavailable")
        return row.source_text, {"scope": scope,
                                 "source": FieldCodec.encode(row.context_provenance()),
                                 "turn_id": row.turn_id}, row

    def source_delivery(self, expected, original, evidence, boundary_entry, fork):
        """Bind authored source to an original input before a cut/probe.

        Branch membership comes from the acquired native reader. A different
        session additionally needs the SDK's original inherited-prefix proof;
        equal copied entry IDs alone cannot establish source identity.
        This observes completed work and grants no next input or replay.
        """
        if original['prompt'] != expected:
            raise ValueError('Original stimulus differs from frozen authored source')
        header, entries = evidence.observe()
        selected = NativeSessionIdentity(header.id, str(evidence.source.path))
        source = original['native_input']
        identities = {source.session_entry_id, self.answer_entry_id}
        if not self.session.same_session(selected):
            if fork is None:
                raise ValueError('Original stimulus lacks matching SDK ancestry')
            fork.source.require_same_session(self.session)
            if not identities <= fork.covered_prefix(evidence, entries):
                raise ValueError('Original stimulus is outside the SDK inherited prefix')
        before = {entry.require_entry_id() for entry in evidence.branch(boundary_entry, entries)[:-1]}
        if not identities <= before:
            raise ValueError('Original stimulus does not precede its selected boundary')
        return {'evaluated': True, 'input': source, 'session': self.session,
                'submitted_source': original['submitted_source'], 'boundary_entry': boundary_entry,
                'scope': 'Exact original authored source and completed native input before the cut/probe; '
                         'not final HTTP bytes, compaction replacement coverage or ACP acknowledgement'}

    @staticmethod
    def answer_for_input(evidence: NativeEvidenceRead, context):
        """Resolve the terminal and its complete original source ancestry.

        Consumers measuring this input's completions select the span after its
        user entry; context construction must retain the inherited ancestry.
        """
        _, entries = evidence.observe()
        user, = (entry for entry in entries if entry.id == context.session_entry_id)
        candidates = []
        for answer in entries[entries.index(user) + 1:]:
            if not answer.final_reply:
                continue
            branch = evidence.branch(answer.id, entries)
            boundaries = tuple(entry for entry in branch if entry.input_boundary)
            if boundaries and boundaries[-1].id == user.id:
                candidates.append((answer, branch))
        answer, = candidates
        return answer

    def read_sdk_context(self):
        """Decode the original external SDK capture at its Pi boundary once."""
        if self.sdk_context is None:
            return None
        data = NativeContextData.from_wire(RecordedNativeCheckpoint.read_json(self.sdk_context))
        self.session.require_same_session(data.identity)
        return data

    @staticmethod
    def sdk_request(data):
        """The captured native provenance selects its original request proof."""
        source, = frozenset(provenance for segment in data.segments
                            for provenance in segment.provenance
                            if isinstance(provenance, NativeProvenance))
        return source

    def request_manifest(self, context, data):
        """One original request relation serves presence and construction."""
        if data is None or self.context_manifest is None:
            return None
        manifest = RecordedNativeCheckpoint.read_record(self.context_manifest, ContextManifest)
        # Raw SDK manifests and wire-public manifests are different views of
        # the same original event. Only that event owns which values were
        # captured: never reconstruct its selection from today's SDK/preview.
        if self.sdk_observation is not None:
            observed = NativeContextManifestData.from_wire(
                RecordedNativeCheckpoint.read_json(self.sdk_observation))
            if observed.for_turn(manifest.thread, manifest.turn) != manifest:
                raise ValueError("Recorded wire public projection differs from its original SDK observation")
            counter, segments = observed.counter, observed.segments
        else:
            # Exact raw metadata proves its existing narrower relation. Missing
            # event evidence cannot authorize a different public projection.
            counter, segments = manifest.counter, manifest.segments
        if (data.counter != counter
                or tuple(segment.measured_manifest() for segment in data.segments) != segments):
            raise ValueError("Recorded SDK payload differs from its original context manifest")
        # Existing NativeProvenance identifies this exact request, not merely a
        # same-session get_context preview. Counter/segment metadata alone do not.
        source = NativeProvenance(self.session, context.request_generation, context.llm_context_digest)
        if not data.segments or any(source not in segment.provenance for segment in data.segments):
            raise ValueError("Recorded SDK context is not this original probe request")
        return manifest

    def serialized_segments(self, data, manifest):
        """Acquire all original SDK serializations against their manifest once.

        Both construction and envelope presence borrow this checked capture.
        Re-encoding decoded segment objects cannot supply missing original bytes.
        """
        if manifest is None:
            return None, {"evaluated": False,
                          "reason": "Original SDK payload and matching context manifest not supplied"}
        if self.sdk_segment_bytes is None:
            return None, {"evaluated": False,
                          "reason": "Original SDK serialized segment bytes not captured; object reserialization is not byte evidence"}
        texts = FieldCodec.decode(tuple[str, ...], RecordedNativeCheckpoint.read_json(self.sdk_segment_bytes))
        if len(texts) != len(data.segments):
            raise ValueError("Recorded SDK serialized segments differ from their manifest")
        for segment, text in zip(data.segments, texts):
            raw = text.encode()
            if (len(raw) != segment.utf8_bytes
                    or hashlib.sha256(raw).hexdigest() != segment.sha256):
                raise ValueError("Recorded SDK segment bytes differ from measured source")
        return texts, {"evaluated": True, "artifact": self.sdk_segment_bytes}

    @staticmethod
    def require_serialized_value(segment, text):
        # JSON type identity matters: Python equality equates true and 1.
        value = json.loads(text, object_pairs_hook=unique_fields)
        if json.dumps(value, sort_keys=True) != json.dumps(segment.provider_value(), sort_keys=True):
            raise ValueError("Recorded SDK segment value differs from its original captured bytes")

    def serialized_messages(self, data, texts):
        """NativeMessages owns the exact input partition, not tool callbacks.

        Callers supply the same acquired serializations, already checked against
        every original digest/length. This projection validates every message
        value, without claiming that other SDK value projections were preserved.
        """
        message_parts=[]
        for segment,text in zip(data.segments,texts):
            if isinstance(segment,NativeMessages):
                self.require_serialized_value(segment, text)
                if not text.startswith('[') or not text.endswith(']'):
                    raise ValueError('Recorded SDK message segment is not its original JSON array')
                if text[1:-1]:
                    message_parts.append(text[1:-1])
        messages='['+','.join(message_parts)+']'
        return {"evaluated": True,
                "provider_messages_sha256": hashlib.sha256(messages.encode()).hexdigest(),
                "scope": "Original SDK message segment bytes/values only; not system/tools, full request, HTTP or capacity"}

    def serialized_construction(self, data, manifest):
        """Whole SDK value agreement, distinct from the message projection."""
        texts, captured = self.serialized_segments(data, manifest)
        if not captured['evaluated']:
            return texts, captured
        for segment, text in zip(data.segments, texts):
            if not isinstance(segment, NativeMessages):
                self.require_serialized_value(segment, text)
        messages = self.serialized_messages(data, texts)
        return texts, {"evaluated": True, "stage": "recorded SDK provider input",
                "artifact": self.sdk_segment_bytes,
                "context_digest": self.sdk_request(data).context_digest,
                "segments": len(data.segments),
                "utf8_bytes": sum(segment.utf8_bytes for segment in data.segments),
                "provider_messages_sha256": messages['provider_messages_sha256'],
                "final_transport_evaluated": False,
                "scope": "Original captured serializations match every SDK measured segment; not HTTP bytes, provider token counts or intervention proof"}

    def prompt_presence(self, retained, texts, captured):
        """Measure envelope presence in the borrowed original SDK capture."""
        if not captured["evaluated"]:
            return captured
        if retained is None or not retained.facts:
            return {"evaluated": False, "reason": "No eligible original retained fact denominator"}
        # Encode the exact envelope as a JSON string because measured native
        # segments contain original provider JSON. This is byte presence, not
        # recall credit, semantic interpretation or final HTTP-body evidence.
        envelope = json.dumps(retained.text, ensure_ascii=False)[1:-1]
        present = any(envelope in text for text in texts)
        return {"evaluated": True, "stage": "recorded SDK provider input",
                "final_transport_evaluated": False,
                "context_digest": captured["context_digest"],
                "required": len(retained.facts),
                "present": len(retained.facts) if present else 0,
                "exact_envelope_present": present}

    def probe_input_presence(self, data, user, captured):
        """Measure rendered probe text in the acquired original SDK request.

        PiMessage owns public text and UserMessage owns input matching. No
        concatenated root, instruction/tool text or assistant echo can supply
        a user-message match. Opaque content prevents an inferred absence;
        positive exact public text remains observable beside opaque siblings.
        This never establishes complete content, HTTP or provider receipt.
        """
        if not captured['evaluated']:
            return {'evaluated': False, 'reason': 'Original matching SDK request bytes unavailable'}
        matches, bound, opaque = [], [], []
        for position, segment in enumerate(data.segments):
            if not isinstance(segment, NativeMessages):
                continue
            for index, raw in enumerate(segment.messages):
                message = PiMessage.from_wire(raw)
                coordinate = (position, index)
                if message.opaque or (message.user and any(part.opaque for part in message.parts)):
                    opaque.append(coordinate)
                if message.user:
                    if message.matches_input(user.message.text, self.input_id, require_id=False):
                        matches.append(coordinate)
                    if message.matches_input(user.message.text, self.input_id, require_id=True):
                        bound.append(coordinate)
        observation = {'matching_user_messages': tuple(matches),
                'matching_native_input_messages': tuple(bound),
                'opaque_messages': tuple(opaque),
                'context_digest': captured['context_digest'],
                'scope': 'Exact rendered native user public text in original serialized SDK messages; '
                         'input-ID matches require the original ID too; no complete-content, '
                         'HTTP submission, provider receipt, intervention or recall claim'}
        if not matches and opaque:
            return dict(observation, evaluated=False,
                        reason='Opaque SDK message content prevents determining rendered input absence')
        return dict(observation, evaluated=True, present=bool(matches))

    def observed_requests(self, manifest, submitted: InputAttempt) -> dict[str, tuple[RequestProgress, ...]]:
        """Acquire this fenced input's original diagnostic values once.

        The manifest owns the selected request's generation/digest and anchors
        the original turn. Other requests retain their own diagnostic identity;
        they never inherit that SDK binding. Budget and timing borrow the same
        original values, with requests/stages in original first-seen order.
        """
        if manifest is None or manifest.request_id is None or self.request_observations is None:
            return {}
        observed = {}
        for record in RecordedNativeCheckpoint.read_json_lines(self.request_observations):
            # Existing diagnostic publications also include parent acquisition
            # records. Only their native member is a RequestProgress boundary.
            if "native" not in record:
                continue
            progress = RequestProgress.from_wire(record["native"])
            if progress.request_id != manifest.request_id and (
                    progress.session_id != self.session.session_id or progress.input_id != self.input_id):
                continue
            lease = FieldCodec.decode(TurnLeaseFence, record["turn"])
            if not manifest.turn.same_recording(RecordedContextTurn(TurnId(lease.turn_id), lease.identity)):
                raise ValueError("Original request observation belongs to another recorded turn")
            if progress.session_id != self.session.session_id or progress.input_id != self.input_id:
                raise ValueError("Original request observation belongs to another native session/input")
            if submitted.exists:
                submitted.require_started(lease.admission_generation)
            observed.setdefault(progress.request_id, []).append(progress)
        return {identity: tuple(points) for identity, points in observed.items()}

    @classmethod
    def input_request_measurements(cls, requests):
        """Export retained requests without manufacturing an input-wide clock.

        Diagnostic capture is optional. Known observations do not establish
        every request/retry, nor SDK/body/terminal binding for earlier requests.
        The selected manifest remains the separate stronger source relation.
        """
        return {'evaluated': bool(requests), 'complete_input_evaluated': False,
                'observed_requests': len(requests),
                'requests': tuple({'request_id': identity,
                    'budget': cls.request_budget(points), 'timing': cls.request_timing(points)}
                    for identity, points in requests.items()),
                'scope': 'Original native request diagnostics with this recorded turn/session/input; '
                         'not complete capture, per-request SDK/HTTP equivalence, provider capacity, '
                         'whole-turn/S1 timing or study acceptance',
                'reason': 'Retained original input request observations' if requests else
                          'Original fenced input request observations unavailable'}

    @staticmethod
    def request_budget(observed: tuple[RequestProgress, ...]):
        """Select original allowances without reacquiring their proof source.

        Provider retries may have multiple admitted allowances. Preserve their
        order; neither the reader nor scorer recomputes a current-model budget.
        """
        admitted = tuple(point for point in observed if point.stage == "budget_admission")
        return {"evaluated": bool(admitted), "observations": tuple(admitted),
                "scope": "Original ContextBudget admission after payload hooks; not provider token counts or HTTP bytes",
                "reason": "Original admitted request calculations" if admitted else "No original budget admission observation"}

    @staticmethod
    def full_history_admission(installation, budget):
        """Join original SDK selection, complete request prefix and admission.

        The native budget alone decides allowance. This reader never computes
        tokens or treats an admitted smaller context as full-history capacity.
        Original provider-token/HTTP capacity remains a separate question.
        """
        selection = installation['entry_selection']
        prefix = installation['constructed_prefix']
        if not selection['evaluated'] or not prefix['evaluated'] or not budget['evaluated']:
            return {'evaluated': False,
                'reason': 'Original selected history, complete bound request prefix and native admission required'}
        full = tuple(value['uncompacted_selection'] for value in selection['observations'])
        if not all(value['evaluated'] for value in full):
            return {'evaluated': False, 'observations': full,
                'reason': 'Original SDK uncompacted selection was not captured'}
        return {'evaluated': True,
            'admitted_full_history': all(value['selected'] for value in full) and prefix['preserved'],
            'selection': full, 'constructed_prefix': prefix, 'request_budget': budget,
            'scope': 'Original SDK uncompacted selection preserved through this bound constructor/request '
                     'prefix and its native estimated admission; not provider-token capacity, HTTP bytes, '
                     'scenario-history equivalence or a matched study'}

    @staticmethod
    def request_timing(observed: tuple[RequestProgress, ...]):
        """Export original native measurements without synthesizing a clock.

        elapsed_ms and callback counters are already measured by the native
        request owner. Stream closure is not turn settlement. Missing stages
        stay missing, including timing for earlier tool-step requests or local
        acquisition/publication whose clock is independently owned.
        """
        return {"evaluated": bool(observed), "observations": observed,
                "whole_turn_evaluated": False,
                "scope": "Original single-request native progress, transport and callback measurements; "
                         "not whole-turn/S1 timing, billed resource use or provider-capacity attribution",
                "reason": "Original correlated request measurements" if observed else
                          "Original correlated request/manifest observations unavailable"}

    @staticmethod
    def request_completion(budget, answer):
        """Join this manifest's admitted model to its original SDK terminal.

        The returned provider model is a separate observation. Registry settings
        or a preceding summary cannot supply absent request/terminal metadata.
        """
        if not budget["evaluated"]:
            return {"evaluated": False, "reason": "Original request admission unavailable"}
        message = answer.message
        if message.provider is None or message.model is None:
            return {"evaluated": False, "reason": "Original SDK terminal selection unavailable"}
        for point in budget["observations"]:
            if point.model is None or point.model.display_name is None:
                return {"evaluated": False, "reason": "Original admitted request model unavailable"}
            if not point.model.matches_identity((message.provider, message.model)):
                return {"evaluated": False,
                        "reason": "Original admitted model does not corroborate this SDK terminal"}
        return {"evaluated": True, "terminal_entry": answer.id,
                "scope": "Correlated ContextBudget model equals this input's final SDK-selected model; not returned-model or HTTP proof"}

    @property
    def checkpoint_source(self):
        """The original cut owns its source; an uncut probe owns only its journal."""
        return Path(self.checkpoint.reference.session_file if self.checkpoint is not None
                    else self.session.session_file)

    @classmethod
    @contextmanager
    def original_readers(cls, probes, checkpoints=()):
        """Acquire each declared original once for this bounded measurement.

        Child and parent must coexist for SDK inheritance corroboration. These
        are only acquired descriptors/decoded bytes; every observation still
        checks its original prefix and every fork still needs its creation.
        """
        paths = dict.fromkeys(chain.from_iterable(
            (Path(probe.session.session_file), probe.checkpoint_source) for probe in probes))
        paths.update(dict.fromkeys(Path(cut.reference.session_file) for cut in checkpoints))
        with ExitStack() as resources:
            yield {path: resources.enter_context(NativeEntry.open_evidence(path)) for path in paths}

    def observe(self):
        with self.original_readers((self,)) as sources:
            return self.read(sources[Path(self.session.session_file)], sources[self.checkpoint_source])

    def construction(self, evidence, parent, branch, manifest, checkpoint, texts, serialized, answer, context, submitted):
        """Corroborate original SDK source references, not a condition label.

        The successful input-to-answer branch owns the available source. A
        later tool-round request may include earlier completions of this input;
        the final answer itself cannot have supplied its preceding SDK context.
        Entry membership is not a claim about transformed provider bytes or
        complete-history capacity. Those need their own original observations.
        """
        journal = self.fork_journal if self.fork_journal is not None else (
            self.checkpoint.journal if self.checkpoint is not None else None)
        fork = None
        if journal is not None:
            fork = CompactionJournal.observe_readonly(journal, lambda db:
                NativeForkCreation.one(db, session_file=self.session.session_file), absent=None)
            if fork is not None:
                fork.require_same_session(self.session)
                fork.covered_prefix(evidence, evidence.entries)
        models = tuple(entry for entry in branch if entry.model_choice is not None)
        thinking = tuple(entry for entry in branch if isinstance(entry, ThinkingLevelChangeEntry))
        coverage = {"evaluated": False, "reason": "Original matching SDK manifest unavailable"}
        if manifest is not None:
            available = evidence.entry_index(branch[:-1])
            included = set()
            segments = []
            for segment in manifest.segments:
                references = tuple(reference for reference in segment.provenance
                                   if isinstance(reference, JournalProvenance))
                identities = []
                for reference in references:
                    if reference.path != self.session.session_file:
                        raise ValueError("SDK source reference belongs to another native session")
                    for identity in reference.entries:
                        if identity not in available:
                            raise ValueError("SDK source reference is outside its original answer branch")
                        identities.append(identity)
                included.update(identities)
                segments.append({"manifest": segment, "source_entries": tuple(identities)})
            messages = tuple(entry.require_entry_id() for entry in branch[:-1] if entry.is_message)
            coverage = {"evaluated": bool(included),
                        "scope": "JournalProvenance entry membership, not transformed message-byte equivalence",
                        "segments": tuple(segments),
                        "original_message_entries": messages,
                        "included_message_entries": tuple(identity for identity in messages if identity in included),
                        "unreferenced_message_entries": tuple(identity for identity in messages if identity not in included),
                        "complete_message_reference_coverage": bool(included) and all(identity in included for identity in messages)}
            if self.checkpoint is not None:
                identity = checkpoint["native_entry_id"]
                coverage["managed_checkpoint"] = {"entry_id": identity,
                    "referenced_in_sdk_sources": identity in included}
        coverage["full_context_capacity"] = {"evaluated": False,
            "reason": "Current request admission does not establish complete-history construction or provider-token capacity"}
        observed_requests = self.observed_requests(manifest, submitted)
        observed_request = observed_requests.get(manifest.request_id, ()) if manifest is not None else ()
        budget = self.request_budget(observed_request)
        records = self.condition_records()
        installation = self.installed_condition(evidence,branch,context,serialized,manifest,records,
                                               parent=parent,texts=texts)
        return {
            "fork": fork,
            "journal_settings": {
                "evaluated": bool(models and thinking),
                "model": models[-1].model_choice if models else None,
                "thinking": FieldCodec.encode(thinking[-1].thinking_level) if thinking else None,
                "original_entries": tuple(entry.id for entry in models[-1:] + thinking[-1:]),
                "scope": "Historical branch metadata; not current request selection",
            },
            "sdk_manifest": manifest,
            "serialized_sdk_source": serialized,
            "request_budget": budget,
            "native_request_timing": self.request_timing(observed_request),
            "input_request_measurements": self.input_request_measurements(observed_requests),
            "request_completion": self.request_completion(budget, answer),
            "source_coverage": coverage,
            "full_history_sdk_admission": self.full_history_admission(installation, budget),
            "condition_application": self.applied_condition(evidence,parent,texts,serialized,manifest,records),
            "condition_installation": installation,
        }

    @staticmethod
    def model_steps(branch):
        """Export every original assistant completion, including tool steps.

        PiUsage owns the external optional counters. Keep their original nulls
        and zeros; a missing usage record is unavailable. NativeEntry owns which
        branch members are assistant messages. This is not a count of transport
        attempts or retries that never produced a journaled assistant record.
        """
        return tuple({
            "entry_id": entry.id,
            "timestamp": entry.timestamp,
            "usage": {
                "evaluated": entry.message.usage is not None,
                "value": entry.message.usage,
            },
            "selection": {
                "evaluated": bool(entry.message.provider and entry.message.model),
                "provider": entry.message.provider,
                "model": entry.message.model,
                "api": entry.message.api,
                "response_model": entry.message.response_model,
                "response_id": entry.message.response_id,
                "provider_thinking_level": entry.message.provider_thinking_level,
                "scope": "Original journaled Pi completion metadata; not an HTTP dispatch receipt or configured effort",
            },
        } for entry in branch if entry.assistant_message)

    def tool_steps(self, branch):
        """Record attempts/results on this input's branch, not inferred actions.

        Pi's decoded result owns exact call matching and successful artifact
        interpretation. This local lookup only joins original journal entries;
        it has no refresh, admission or execution authority. A missing result
        stays unavailable, and an error result never becomes success.
        """
        pending = {}
        measured = []
        for entry in branch:
            for call in entry.retained_tool_calls():
                if call.id in pending:
                    raise ValueError("Recorded probe repeats an outstanding SDK tool call")
                observation = {"source": JournalProvenance(self.session.session_file, (entry.require_entry_id(),)),
                    "call": call,
                    "completion": {"evaluated": False, "reason": "No original SDK result on this probe branch"}}
                measured.append(observation)
                pending[call.id] = (entry, observation)
            if isinstance(entry, MessageEntry) and isinstance(entry.message, ToolResultMessage):
                message = entry.message
                try:
                    request, observation = pending.pop(message.tool_call_id)
                except KeyError as error:
                    raise ValueError("Recorded tool result lacks one preceding original request") from error
                message.require_tool_request(request)
                observation.update(source=JournalProvenance(self.session.session_file,
                                      (request.require_entry_id(), entry.require_entry_id())),
                    completion={"evaluated": True, "successful": not message.is_error,
                                "artifacts": message.completed_artifacts()})
        return tuple(measured)

    def read(self, evidence: NativeEvidenceRead, source: NativeEvidenceRead):
        """Borrow the run owner's original source for every measurement."""
        evidence.require_path(Path(self.session.session_file))
        data = self.read_sdk_context()
        context = NativeContextProof.read_evidence(
            Path(self.session.session_file), self.input_id, evidence=evidence,
            request_generation=self.sdk_request(data).request_generation if data is not None else None,
        )
        self.session.require_same_session(NativeSessionIdentity(
            context.session_id, str(context.session_file)
        ))
        _, entries = evidence.observe()
        user, = (row for row in entries if row.id == context.session_entry_id)
        manifest = self.request_manifest(context, data)
        texts, serialized = self.serialized_construction(data, manifest)
        answer, = (row for row in entries if row.id == self.answer_entry_id)
        if not isinstance(answer, MessageEntry) or not answer.final_reply:
            raise ValueError("Recorded recall answer is not a successful native terminal")
        original, source_branch = self.answer_for_input(evidence, context)
        if answer.id != original.id:
            raise ValueError("Recorded answer belongs to another original input")
        branch = source_branch[source_branch.index(user) + 1:]
        tools = self.tool_steps(branch)
        prompt, submitted, submitted_input = self.submitted_prompt(user, manifest)
        if self.checkpoint is not None:
            attempt, entry, covered, assembly = self.checkpoint.capture_for_probe(
                self.session,evidence,self.fork_journal,source,user)
            checkpoint = self.checkpoint._report(attempt, entry, covered, assembly)
            retained = attempt.request.retained
            scoped = checkpoint["scoped_facts"]
        else:
            retained = None
            scoped = {"evaluated": False, "reason": "No original scoped checkpoint",
                      "configured_settings": {"evaluated": False}}
            checkpoint = {
                "applicable": False, "reason": "No compaction checkpoint declared for this control",
                "canonical_availability": {
                    "evaluated": False, "reason": "Full-context control has no committed retained envelope"
                },
            }
        return {
            # The located proof's Path is an acquired resource coordinate.
            # Export its original declared wire facts, not a second proof.
            "context": FieldCodec.encode({
                item.metadata.get("wire_name", item.name): getattr(context, item.name)
                for item in fields(NativeContextRecord)
            }),
            "session": FieldCodec.encode(self.session),
            "native_input": context.input_commit,
            "prompt": prompt,
            "submitted_source": submitted,
            "answer": FieldCodec.encode(answer),
            "answer_text": answer.message.authoritative_text,
            "model_steps": self.model_steps(branch),
            "tool_steps": tools,
            "construction": self.construction(evidence, source, source_branch, manifest, checkpoint, texts, serialized, answer, context, submitted_input),
            "scoped_facts": scoped,
            "answer_support": {
                "tool_calls": len(tools), "tools": tuple(step["call"].name for step in tools),
                "unassisted_recall": not tools,
                "scope": "Original probe branch; tool-assisted answers are task quality, not unassisted recall",
            },
            "checkpoint": checkpoint,
            "canonical_availability": checkpoint["canonical_availability"],
            "provider_prompt_presence": self.prompt_presence(retained, texts, serialized),
            "probe_input_presence": self.probe_input_presence(data, user, serialized),
            "prompt_scope": "original native user and assembled-context proof, not final provider payload",
        }



def retained_native_host(
    proc, launch, identity: NativeSessionIdentity, *, reader=None, stderr_task=None
):
    child = PiSessionChild(
        proc,
        reader if reader is not None else PiRpcChannel(proc.stdout),
        (
            stderr_task
            if stderr_task is not None
            else asyncio.create_task(PiSessionChild.stderr_tail(proc.stderr))
        ),
        (launch, (0, 0)),
        ObservedAttestation(
            StateData(session_id=identity.session_id, session_file=identity.session_file)
        ),
    )
    persistent = PersistentPiSession()
    assert persistent.retain(child, identity)
    return persistent
