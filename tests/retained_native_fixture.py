"""Read original retention measurements and reuse the actual native custody owner."""

import asyncio
import hashlib
import json
from collections import Counter
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Annotated

from agent_comms.compaction_identity import SummaryOperationIdentity
from agent_comms.compaction_journal import CompactionJournal
from agent_comms.compaction_records import CompactionOperation, NativeForkCreation, SelectedSummaryAttempt
from agent_comms.field_codec import FieldCodec, PathText
from agent_comms.input_disposition import InputDocument
from agent_comms.native_entries import ManagedCompactionEntry, MessageEntry, NativeEntry, NativeEvidenceRead, ThinkingLevelChangeEntry
from agent_comms.native_input_record import NativeInputIdText
from agent_comms.native_pi import NativeContextProof, NativeContextRecord

from agent_comms.backend import PersistentPiSession
from agent_comms.native_attestation import ObservedAttestation
from agent_comms.native_custody import PiSessionChild
from agent_comms.native_session_reopen import NativeSessionIdentity
from agent_comms.pi_payloads import StateData, ToolResultMessage
from agent_comms.pi_rpc import PiRpcChannel
from agent_comms.pi_rpc import unique_fields
from agent_comms.native_turn_context import NativeContextData
from agent_comms.registry_document import RegistryDocument
from agent_comms.request_progress import RequestProgress
from agent_comms.turn_lease import TurnLeaseFence
from agent_comms.thread_identity import TurnId
from agent_comms.retained_task_facts import (
    ConstraintTaskFact, DecisionTaskFact, HumanConstraintTaskFact, RetainedTaskFacts,
)
from agent_comms.turn_context import ContextManifest, FileProvenance, JournalProvenance, NativeProvenance, RecordedContextTurn
from agent_comms.wire_log import WireLog


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

    @staticmethod
    def read_bytes(reference: FileProvenance):
        """Read unchanged original bytes before their declared boundary decodes."""
        raw = Path(reference.path).read_bytes()
        if hashlib.sha256(raw).hexdigest() != reference.sha256:
            raise ValueError("Recorded measurement artifact changed")
        return raw

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
        return {"evaluated": True, "usage": FieldCodec.encode(entry.usage),
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
            return attempt, entry, covered

        original = CompactionJournal.observe_readonly(self.journal, read, absent=None)
        if original is None:
            raise ValueError("Recorded checkpoint has no original compaction journal")
        return original

    def _report(self, attempt: SelectedSummaryAttempt, entry: ManagedCompactionEntry,
               covered: frozenset[str]):
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

    def inspect(self, previous: RecordedNativeCheckpoint | None = None):
        """Read a checkpoint or adjacent-cut difference without a new model input.

        The optional previous reference is external evaluation input. It neither
        selects a live source nor carries native lifecycle/admission state.
        """
        with NativeEntry.open_evidence(Path(self.reference.session_file)) as evidence:
            header, _ = evidence.observe()
            session = NativeSessionIdentity(header.id, str(evidence.source.path))
            current_attempt, current_entry, covered = self.capture(session, evidence)
            report = self._report(current_attempt, current_entry, covered)
            if previous is not None:
                previous_attempt, previous_entry, _ = previous.capture(session, evidence)
                branch = evidence.branch(current_entry.id, evidence.entries)
                if previous_entry.id == current_entry.id or previous_entry not in branch:
                    raise ValueError("Checkpoint comparison requires distinct original ancestor cuts")
                report["revision_mass"] = self.revision_from(previous, previous_attempt, current_attempt)
                report["source_changes"] = {
                    "previous": FieldCodec.encode(previous_attempt.identity),
                    "current": FieldCodec.encode(current_attempt.identity),
                    **current_attempt.request.retained.changed_from(previous_attempt.request.retained),
                }
            return report

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
        with WireLog(self.wire).certified_read() as source:
            for fact in attempt.request.retained.facts:
                for message in fact.authored_sources():
                    delivery, = source.references((message.reference,))
                    if delivery.message != message:
                        raise ValueError("Retained authored fact differs from its original publication")
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
        """Read original captured configuration and scoped Decision publications."""
        captured = self.authored_scope(attempt)
        if captured is None:
            return {"evaluated": False, "reason": "Original scope/publications unavailable",
                    "configured_settings": {"evaluated": False}}
        snapshot, owner = captured
        retained = attempt.request.retained
        declarations = self.authored_lineages(retained, owner, snapshot, DecisionTaskFact)
        return {"evaluated": True, "scope": "Original scoped Decision alternatives only",
                "configured_settings": {"evaluated": owner.model is not None and owner.thinking_level is not None,
                                        "model": owner.model, "thinking": FieldCodec.encode(owner.thinking_level),
                                        "scope": "Captured registry configuration, not provider-reported request selection"},
                "decisions": tuple({"lineage": root.reference,
                                    "current": message.reference,
                                    "declaration": message.task.require_decision()}
                                   for root, message in declarations)}


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

    def submitted_prompt(self, user):
        """Bind an original submitted source to its exact recorded native write.

        A direct-native control measures its native user text. An ACP capture
        supplies the original InputDocument, whose STARTED member owns both
        submitted and rendered text. This is measurement, never lease authority.
        """
        if self.submitted_inputs is None:
            return user.message.text, {"scope": "original native user text"}
        document = RecordedNativeCheckpoint.read_record(self.submitted_inputs, InputDocument)
        row, = (row for row in document.rows.values()
                if row.has_started and row.native_id == self.input_id)
        if not row.matches_native(turn_id=row.turn_id, native_id=self.input_id, text=user.message.text):
            raise ValueError("Original submitted input differs from the recorded native write")
        return row.source_text, {"scope": "original STARTED InputDocument source and exact sent text",
                                 "source": FieldCodec.encode(row.context_provenance()),
                                 "turn_id": row.turn_id}

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
        if (data.counter != manifest.counter
                or tuple(segment.measured_manifest() for segment in data.segments) != manifest.segments):
            raise ValueError("Recorded SDK payload differs from its original context manifest")
        # Existing NativeProvenance identifies this exact request, not merely a
        # same-session get_context preview. Counter/segment metadata alone do not.
        source = NativeProvenance(self.session, context.request_generation, context.llm_context_digest)
        if not data.segments or any(source not in segment.provenance for segment in data.segments):
            raise ValueError("Recorded SDK context is not this original probe request")
        return manifest

    def serialized_construction(self, data, manifest):
        """Acquire original SDK bytes independently of a retained-fact oracle.

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
        return texts, {"evaluated": True, "stage": "recorded SDK provider input",
                "artifact": self.sdk_segment_bytes,
                "context_digest": self.sdk_request(data).context_digest,
                "segments": len(data.segments),
                "utf8_bytes": sum(segment.utf8_bytes for segment in data.segments),
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

    def request_budget(self, manifest):
        """Bind admitted calculations through the original manifest request ID.

        The manifest already owns the exact native generation/digest relation.
        No same-input, time or present-day catalog join can replace this link.
        Provider retries can produce more than one original admitted allowance;
        preserve their order instead of manufacturing one final request value.
        """
        if manifest is None or manifest.request_id is None or self.request_observations is None:
            return {"evaluated": False, "reason": "Original correlated request/manifest observations unavailable"}
        admitted = []
        for record in RecordedNativeCheckpoint.read_json_lines(self.request_observations):
            # Existing diagnostic publications also include parent acquisition
            # records. Only their native member is a RequestProgress boundary.
            if "native" not in record:
                continue
            progress = RequestProgress.from_wire(record["native"])
            if progress.request_id != manifest.request_id:
                continue
            lease = FieldCodec.decode(TurnLeaseFence, record["turn"])
            if not manifest.turn.same_recording(RecordedContextTurn(TurnId(lease.turn_id), lease.identity)):
                raise ValueError("Original request observation belongs to another recorded turn")
            if progress.session_id != self.session.session_id or progress.input_id != self.input_id:
                raise ValueError("Original request observation belongs to another native session/input")
            if progress.stage == "budget_admission":
                admitted.append(progress)
        return {"evaluated": bool(admitted), "observations": tuple(admitted),
                "scope": "Original ContextBudget admission after payload hooks; not provider token counts or HTTP bytes",
                "reason": "Original admitted request calculations" if admitted else "No original budget admission observation"}

    def observe(self):
        with NativeEntry.open_evidence(Path(self.session.session_file)) as evidence:
            return self.read(evidence)

    def construction(self, evidence, branch, manifest, checkpoint, serialized):
        """Corroborate original SDK source references, not a condition label.

        The successful input-to-answer branch owns the available source. A
        later tool-round request may include earlier completions of this input;
        the final answer itself cannot have supplied its preceding SDK context.
        Entry membership is not a claim about transformed provider bytes or
        complete-history capacity. Those need their own original observations.
        """
        journal = self.checkpoint.journal if self.checkpoint is not None else self.fork_journal
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
                        "complete_message_reference_coverage": bool(included) and all(identity in included for identity in messages),
                        "full_context_capacity": {"evaluated": False,
                            "reason": "Current request admission does not establish complete-history construction or provider-token capacity"}}
            if self.checkpoint is not None:
                identity = checkpoint["native_entry_id"]
                coverage["managed_checkpoint"] = {"entry_id": identity,
                    "referenced_in_sdk_sources": identity in included}
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
            "request_budget": self.request_budget(manifest),
            "source_coverage": coverage,
            "condition_evaluated": False,
            "reason": "SDK assembly is recorded; a label does not prove full-history or truncation policy",
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
                "value": FieldCodec.encode(entry.message.usage),
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
                    "call": FieldCodec.encode(call),
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
                                "artifacts": FieldCodec.encode(message.completed_artifacts())})
        return tuple(measured)

    def read(self, evidence: NativeEvidenceRead):
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
        prompt, submitted = self.submitted_prompt(user)
        if self.checkpoint is not None:
            attempt, entry, covered = self.checkpoint.capture(self.session, evidence)
            checkpoint = self.checkpoint._report(attempt, entry, covered)
            retained = attempt.request.retained
            scoped = self.checkpoint.scoped_facts(attempt)
            if user.parent_id != checkpoint["native_entry_id"]:
                raise ValueError("Recorded probe must immediately follow its original checkpoint")
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
            "prompt": prompt,
            "submitted_source": submitted,
            "answer": FieldCodec.encode(answer),
            "answer_text": answer.message.authoritative_text,
            "model_steps": self.model_steps(branch),
            "tool_steps": tools,
            "construction": self.construction(evidence, source_branch, manifest, checkpoint, serialized),
            "scoped_facts": scoped,
            "answer_support": {
                "tool_calls": len(tools), "tools": tuple(step["call"]["name"] for step in tools),
                "unassisted_recall": not tools,
                "scope": "Original probe branch; tool-assisted answers are task quality, not unassisted recall",
            },
            "checkpoint": checkpoint,
            "canonical_availability": checkpoint["canonical_availability"],
            "provider_prompt_presence": self.prompt_presence(retained, texts, serialized),
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
