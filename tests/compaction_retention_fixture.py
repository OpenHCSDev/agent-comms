"""Frozen recall scoring and original-record repeated-compaction measurements.

Run directly to export public history/questions. --answers scores supplied JSON;
--native-probes reads original input/context/answer/checkpoint references.
--native-checkpoint measures an original committed cut without a model call.
--recorded-run measures declared original cuts and recorded probes together.
Oracle metadata is omitted from exported questions.
Exact-match scoring deliberately measures identifiers/state, not prose quality.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
import argparse
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from enum import Enum
from itertools import chain
import json
from pathlib import Path

from agent_comms.field_codec import FieldCodec
from agent_comms.pi_rpc import unique_fields
from agent_comms.native_entries import NativeEntry
from agent_comms.native_session_reopen import NativeSessionIdentity
from agent_comms.message_reference import MessageReference
from agent_comms.turn_context import JournalProvenance, ToolCatalogSegment
from retained_native_fixture import RecordedNativeCheckpoint, RecordedNativeProbe


@dataclass(frozen=True)
class RecordedAnswers:
    rounds: dict[str, dict[str, str]]


@dataclass(frozen=True)
class RecordedNativeProbes:
    rounds: dict[str, RecordedNativeProbe]
    checkpoints: dict[str, RecordedNativeCheckpoint] = field(default_factory=dict)

    def resume_fork(self, journal, inputs):
        """Corroborate recorded work before a distinct continuation, never replay it.

        This grants no input admission or ACP acknowledgement. The registry's
        idle lease and the normal configured session still own the next turn.
        """
        from agent_comms.compaction_records import NativeForkCreation

        selected = dict(self.checkpoints)
        for identity, probe in self.rounds.items():
            if probe.checkpoint is None or identity in selected:
                raise ValueError("Continuation requires each original cut exactly once")
            selected[identity] = probe.checkpoint
        if not selected:
            raise ValueError("Continuation requires original committed work")
        paths = {cut.reference.session_file for cut in selected.values()}
        path, = paths
        if any(cut.journal != journal.path for cut in selected.values()):
            raise ValueError("Continuation belongs to another compaction journal")
        with journal.transaction() as db:
            fork = NativeForkCreation.one(db, session_file=path)
        if fork is None:
            raise ValueError("Continuation has no original enrolled fork")
        session = NativeSessionIdentity(fork.session_id, fork.session_file)
        document = inputs.read()
        if {row.native_id for row in document.rows.values() if row.has_started} != {
                probe.input_id for probe in self.rounds.values()} or len(document.rows) != len(self.rounds):
            raise ValueError("Continuation cannot include an unrecorded or uncertain input")
        with NativeEntry.open_evidence(Path(path)) as evidence:
            _, entries = evidence.observe()
            terminals = []
            for cut in selected.values():
                _, entry, _ = cut.capture(session, evidence)
                terminals.append(entry)
            for probe in self.rounds.values():
                session.require_same_session(probe.session)
                probe.read(evidence)
                if probe.sdk_context is None or probe.submitted_inputs is None:
                    raise ValueError("Continuation requires the original SDK request capture")
                from agent_comms.input_disposition import InputDocument
                original = RecordedNativeCheckpoint.read_record(probe.submitted_inputs, InputDocument)
                _, current = evidence.observe()
                row, = (row for row in document.rows.values() if row.native_id == probe.input_id)
                if original.lookup(row.key) != row:
                    raise ValueError("Continuation differs from its original STARTED input")
                terminals.append(next(entry for entry in current if entry.id == probe.answer_entry_id))
            _, current = evidence.observe()
            branch = evidence.branch(current[-1].id, current)
            if current[-1] not in terminals or any(entry not in branch for entry in terminals):
                raise ValueError("Continuation source has unrecorded work or different ancestry")
        return fork

    def observe(self, rounds):
        """Visit declared cuts in frozen round order, borrowing each source once."""
        if self.rounds.keys() & self.checkpoints.keys():
            raise ValueError("A probed round's checkpoint belongs on its RecordedNativeProbe")
        selected = dict(self.checkpoints)
        for identity, probe in self.rounds.items():
            if probe.checkpoint is not None:
                selected[identity] = probe.checkpoint
        unexpected = selected.keys() - {item.identity for item in rounds}
        if unexpected:
            raise ValueError(f"Unknown checkpoint rounds: {sorted(unexpected)}")
        cuts = tuple((item.identity, selected[item.identity]) for item in rounds
                     if item.identity in selected)
        if not cuts and not self.rounds:
            return {}, {}
        path = Path(cuts[0][1].reference.session_file) if cuts else Path(
            next(iter(self.rounds.values())).session.session_file
        )
        reports, observations = {}, {}
        with NativeEntry.open_evidence(path) as evidence:
            header, _ = evidence.observe()
            session = NativeSessionIdentity(header.id, str(path))
            previous = None
            for identity, checkpoint in cuts:
                attempt, entry, covered = checkpoint.capture(session, evidence)
                report = checkpoint._report(attempt, entry, covered)
                if previous is not None:
                    old, prior_attempt, prior_entry = previous
                    if prior_entry.id == entry.id or prior_entry not in evidence.branch(entry.id, evidence.entries):
                        raise ValueError("Repeated measurements require distinct original ancestor cuts")
                    report["source_changes"] = attempt.request.retained.changed_from(prior_attempt.request.retained)
                    report["revision_mass"] = checkpoint.revision_from(old, prior_attempt, attempt)
                reports[identity] = report
                previous = checkpoint, attempt, entry
            for identity, probe in self.rounds.items():
                session.require_same_session(probe.session)
                observations[identity] = probe.read(evidence)
        return reports, observations

    def alignment(self, other, observations, baseline, rounds):
        """Compare acquired original facts, never regenerate a control history."""
        pairs = {}
        for round_ in rounds:
            identity = round_.identity
            if identity not in observations or identity not in baseline:
                pairs[identity] = {"evaluated": False, "reason": "One original probe is missing"}
                continue
            original, control = observations[identity], baseline[identity]
            if (self.rounds[identity].session.same_session(other.rounds[identity].session)
                    or self.rounds[identity].input_id == other.rounds[identity].input_id):
                raise ValueError("A matched control cannot reuse the candidate's original session/input")
            a, b = original["construction"], control["construction"]
            if a["fork"] is None or b["fork"] is None:
                pairs[identity] = {"evaluated": False, "reason": "Original SDK fork records unavailable"}
                continue
            a["fork"].source.require_same_session(b["fork"].source)
            if a["fork"].source_revision != b["fork"].source_revision:
                raise ValueError("Matched probes have different original source revisions")
            settings = original["scoped_facts"]["configured_settings"], control["scoped_facts"]["configured_settings"]
            if not all(item["evaluated"] for item in settings):
                pairs[identity] = {"evaluated": False, "reason": "Original configured model/effort unavailable"}
                continue
            for name in ("model", "thinking"):
                if settings[0][name] != settings[1][name]:
                    raise ValueError("Matched probes have different configured model/effort")
            if a["sdk_manifest"] is None or b["sdk_manifest"] is None:
                pairs[identity] = {"evaluated": False, "reason": "Original request manifests unavailable"}
                continue
            if a["sdk_manifest"].counter != b["sdk_manifest"].counter:
                raise ValueError("Matched probes use different native measurement counters")
            catalogs = tuple(tuple(segment for segment in item["sdk_manifest"].segments
                                   if segment.kind == ToolCatalogSegment.declared_name)
                             for item in (a, b))
            if not all(catalogs):
                pairs[identity] = {"evaluated": False, "reason": "Original tool catalogs unavailable"}
                continue
            if tuple((item.sha256, item.utf8_bytes) for item in catalogs[0]) != tuple(
                    (item.sha256, item.utf8_bytes) for item in catalogs[1]):
                raise ValueError("Matched probes have different native tool catalogs")
            request = self.request_alignment(original, control, settings[0]["model"])
            completion = self.completion_alignment(original, control)
            pairs[identity] = {
                "evaluated": request["evaluated"] and completion["evaluated"],
                "scope": "Common original SDK fork source, configured selection, admitted request model, journaled completion selections, tool catalog and frozen probe; not complete intervention/construction proof",
                "request_selection": request,
                "completion_selection": completion,
                "sdk_manifest_changes": a["sdk_manifest"].changed_since(b["sdk_manifest"]),
                "reason": request["reason"] if not request["evaluated"] else (
                    "Original completion selections unavailable" if not completion["evaluated"]
                    else "Original request and completion selections corroborate the source match"),
            }
        return pairs

    @staticmethod
    def request_alignment(original, control, selection):
        """Consume the probe's exact admitted request, never infer it from config.

        ContextBudget owns every estimate/allowance. Its recorded model owns
        selection; this comparison does not recalculate capacity or turn an
        absent output intent into an equal zero. Revised allowances remain in
        their original order and need not match between different histories.
        """
        groups = tuple(observed["construction"]["request_budget"]
                       for observed in (original, control))
        if not all(group["evaluated"] for group in groups):
            return {"evaluated": False, "reason": "Original correlated request admission unavailable"}
        points = tuple(group["observations"] for group in groups)
        if not all(points):
            raise ValueError("An evaluated request admission requires its original observations")
        models = []
        for group in points:
            selected = []
            for point in group:
                if point.model is None or point.model.display_name is None:
                    return {"evaluated": False, "reason": "Original admitted request model unavailable"}
                selected.append(point.model.require_selection(selection))
            models.append(tuple(selected))
        capacity = {
            "context_window": RecordedNativeProbes.same_observations("request context window", tuple(
                tuple(model.context_window for model in group) for group in models)),
            "max_tokens": RecordedNativeProbes.same_observations("request model output capacity", tuple(
                tuple(model.max_tokens for model in group) for group in models)),
        }
        contract = {
            "output_token_field": RecordedNativeProbes.same_observations("request output field", tuple(
                (group[0].output_token_field,) for group in points)),
            "requested_output_tokens": RecordedNativeProbes.same_observations("request initial output intent", tuple(
                (group[0].requested_output_tokens,) for group in points)),
            "minimum_output_tokens": RecordedNativeProbes.same_observations("request minimum output", tuple(
                (group[0].minimum_output_tokens,) for group in points)),
        }
        return {"evaluated": True, "reason": "Original admitted request models match the captured selection",
                "model_capacity": capacity, "request_contract": contract,
                "candidate": groups[0], "baseline": groups[1],
                "scope": "Original native admission observations; not full-history capacity, HTTP bytes or returned model/effort"}

    @staticmethod
    def same_observations(name, groups):
        """One matched-field algorithm preserves absence and original values."""
        available = all(groups) and all(value is not None for group in groups for value in group)
        same = set(groups[0]) == set(groups[1]) if available else None
        if available and not same:
            raise ValueError(f"Matched original {name} observations differ")
        return {"evaluated": available, "same": same,
                "candidate": groups[0], "baseline": groups[1]}

    @staticmethod
    def completion_alignment(original, control):
        """Original completion records own this observation, never settings."""
        groups = tuple(tuple(step["selection"] for step in observed["model_steps"])
                       for observed in (original, control))
        if not all(groups) or not all(item["evaluated"] for group in groups for item in group):
            return {"evaluated": False, "reason": "Original completion selections unavailable"}
        choices = tuple(frozenset((item["provider"], item["model"]) for item in group)
                        for group in groups)
        if choices[0] != choices[1]:
            raise ValueError("Matched probes have different journaled completion models")
        # APIs and returned-model/effort fields are independent original facts.
        # Missing fields cannot be supplied by historical or registry settings.
        fields = {}
        for name in ("api", "response_model", "provider_thinking_level"):
            values = tuple(tuple(item[name] for item in group) for group in groups)
            fields[name] = RecordedNativeProbes.same_observations(f"completion {name}", values)
        return {"evaluated": True, "models": tuple(sorted(choices[0])), "fields": fields,
                "scope": "Original journaled Pi completion observations, not transport attempt receipts"}


class Condition(str, Enum):
    """Value-only experimental labels."""

    FULL_CONTEXT = "full-context"
    BOUNDED = "bounded"
    TASK_MEMORY = "task-memory"
    RECENT_ONLY = "recent-only"


class Measurement(str, Enum):
    """Report labels; all use the same frozen exact-answer scoring algorithm."""

    RECALL = "recall"
    PROHIBITION = "prohibition"
    ALTERNATIVE = "alternative"
    ACTION = "action"


@dataclass(frozen=True)
class AnswerScore:
    correct: bool
    stale: bool
    missing: bool


class ScoreView(ABC):
    """Derive every aggregate from owned outcomes, never stored count replicas."""

    @property
    @abstractmethod
    def scored_answers(self) -> Iterator[tuple[Question, AnswerScore]]:
        """Keep the original question beside its outcome through every aggregate."""

    @property
    def outcomes(self) -> Iterator[AnswerScore]:
        return (outcome for _, outcome in self.scored_answers)

    @property
    def questions(self) -> int:
        return self.public_totals()["questions"]

    @property
    def correct(self) -> int:
        return self.public_totals()["correct"]

    @property
    def stale(self) -> int:
        return self.public_totals()["stale"]

    @property
    def missing(self) -> int:
        return self.public_totals()["missing"]

    def public_totals(self) -> dict:
        return self.totals(self.outcomes)

    @staticmethod
    def totals(outcomes: Iterator[AnswerScore]) -> dict:
        measured = tuple(outcomes)
        return {
            "questions": len(measured),
            "correct": sum(outcome.correct for outcome in measured),
            "stale": sum(outcome.stale for outcome in measured),
            "missing": sum(outcome.missing for outcome in measured),
        }

    def measurement_totals(self) -> dict:
        grouped = {}
        for question, outcome in self.scored_answers:
            grouped.setdefault(question.measurement, []).append(outcome)
        return {measurement.value: self.totals(iter(outcomes))
                for measurement, outcomes in grouped.items()}


@dataclass(frozen=True)
class ScoredRound(ScoreView):
    source: RecallRound
    answer_values: dict[str, str]

    def __post_init__(self):
        unexpected = self.answer_values.keys() - {question.identity for question in self.source.questions}
        if unexpected:
            raise ValueError(f"Unknown questions in {self.identity}: {sorted(unexpected)}")

    @property
    def identity(self) -> str:
        return self.source.identity

    @property
    def scored_answers(self) -> Iterator[tuple[Question, AnswerScore]]:
        return ((question, question.score(self.answer_values.get(question.identity)))
                for question in self.source.questions)

    def public(self) -> dict:
        return {
            "round": self.identity,
            **self.public_totals(),
            "measurements": self.measurement_totals(),
            "answers": {question.identity: asdict(outcome) for question, outcome in self.scored_answers},
        }


@dataclass(frozen=True)
class ScoredScenario(ScoreView):
    source: RecallScenario
    condition: Condition
    rounds: tuple[ScoredRound, ...]

    def __post_init__(self):
        if tuple(round_.source for round_ in self.rounds) != self.source.rounds:
            raise ValueError("Scored rounds must preserve the complete frozen scenario order")

    @property
    def identity(self) -> str:
        return self.source.identity

    @property
    def scored_answers(self) -> Iterator[tuple[Question, AnswerScore]]:
        return chain.from_iterable(item.scored_answers for item in self.rounds)

    def public(self) -> dict:
        return {
            "scenario": self.identity,
            "condition": self.condition.value,
            "oracle_origin": "authored-frozen",
            "answer_origin": "authored-control",
            "rounds": [item.public() for item in self.rounds],
            **self.public_totals(),
            "measurements": self.measurement_totals(),
        }

    def support_groups(self, *evidence):
        """One classification serves individual and paired denominators."""
        grouped = {"unassisted_recall": [], "tool_assisted_task_quality": [], "unobserved": []}
        for round_ in self.rounds:
            if any(round_.identity not in originals for originals in evidence):
                group = "unobserved"
            else:
                group = ("unassisted_recall" if all(originals[round_.identity]["answer_support"]["unassisted_recall"]
                                                   for originals in evidence)
                         else "tool_assisted_task_quality")
            grouped[group].append(round_)
        return grouped

    def support_totals(self, evidence):
        """Keep frozen denominators when native evidence is missing or assisted."""
        return {name: self.totals(chain.from_iterable(round_.outcomes for round_ in rounds))
                for name, rounds in self.support_groups(evidence).items()}

    def proposed_actions(self, evidence):
        return {round_.identity: {question.identity: question.proposed_action(
                    round_.answer_values.get(question.identity), evidence.get(round_.identity))
                    for question, _ in round_.scored_answers if question.measurement is Measurement.ACTION}
                for round_ in self.rounds}

    def public_native(self, checkpoints, evidence) -> dict:
        result = self.public()
        return FieldCodec.encode(dict(result, native_probes=evidence,
                    answer_origin="recorded-native",
                    quality_denominators=self.support_totals(evidence),
                    proposed_actions=self.proposed_actions(evidence),
                    scope="recorded original native probes; condition label is not construction proof",
                    checkpoints=checkpoints,
                    original_checkpoint_count=len(checkpoints),
                    three_original_cuts_observed=len(checkpoints) >= 3,
                    canonical_availability={
                        item.identity: checkpoints[item.identity]["canonical_availability"]
                        if item.identity in checkpoints else {
                            "evaluated": False, "reason": "No original committed checkpoint supplied"
                        } for item in self.source.rounds
                    },
                    provider_prompt_presence={
                        item.identity: evidence[item.identity]["provider_prompt_presence"]
                        if item.identity in evidence else {
                            "evaluated": False, "reason": "No original native probe supplied"
                        } for item in self.source.rounds
                    },
                    revision_mass={identity: report["revision_mass"]
                                   for identity, report in checkpoints.items()},
                    answer_support={identity: original["answer_support"]
                                    for identity, original in evidence.items()},
                    recall_scope="Original recorded answers; tool-assisted answers are task quality, not unassisted recall. Authored answers are scorer controls"))

    def paired_quality(self, baseline, evidence, baseline_evidence, alignment):
        """Original alignment owns pairing; every frozen question stays visible."""
        if self.source != baseline.source:
            raise ValueError("Matched quality requires the same complete frozen oracle")
        controls = {round_.identity: round_ for round_ in baseline.rounds}
        grouped = self.support_groups(evidence, baseline_evidence)
        unmatched = []
        for name, rounds in grouped.items():
            if name == "unobserved":
                continue
            matched = []
            for round_ in rounds:
                (matched if alignment[round_.identity]["evaluated"] else unmatched).append(round_)
            grouped[name] = matched
        grouped["unmatched"] = unmatched
        measured = {}
        for name, rounds in grouped.items():
            candidate = self.totals(chain.from_iterable(round_.outcomes for round_ in rounds))
            control = self.totals(chain.from_iterable(controls[round_.identity].outcomes for round_ in rounds))
            evaluated = bool(rounds) and all(alignment[round_.identity]["evaluated"] for round_ in rounds)
            measured[name] = {"candidate": candidate, "baseline": control,
                              "evaluated": evaluated,
                              "correct_difference": candidate["correct"] - control["correct"]
                                                    if evaluated else None,
                              "scope": "Descriptive differences for matched original source and selections; not certified interventions or study margins"}
        return measured


@dataclass(frozen=True)
class Question:
    identity: str
    prompt: str
    expected: str
    evidence_ref: str
    obsolete: tuple[str, ...] = ()
    measurement: Measurement = Measurement.RECALL
    # Optional original source coordinate in external evaluation input. The
    # authored evidence_ref label cannot authenticate a runtime Decision.
    decision_source: MessageReference | None = None
    # Original execution coordinates, distinct from an authored answer or a
    # Decision alternative. This does not assert proposal/constraint validity.
    action_source: JournalProvenance | None = None

    def __post_init__(self) -> None:
        if not self.identity or not self.prompt or not self.evidence_ref:
            raise ValueError("A frozen question requires identity, prompt and original evidence")
        if self.expected in self.obsolete:
            raise ValueError("A current expected answer cannot also be obsolete")
        if self.decision_source is not None and self.measurement is not Measurement.ACTION:
            raise ValueError("A Decision proposal source belongs on an action question")
        if self.action_source is not None:
            if self.measurement is not Measurement.ACTION:
                raise ValueError("An execution source belongs on an action question")
            if len(self.action_source.entries) != 2 or len(set(self.action_source.entries)) != 2:
                raise ValueError("An execution source requires distinct original request/result entries")

    def score(self, answer: str | None) -> AnswerScore:
        return AnswerScore(
            correct=answer == self.expected,
            stale=answer in self.obsolete,
            missing=answer is None,
        )

    def public(self) -> dict:
        return {"id": self.identity, "prompt": self.prompt}

    def proposed_action(self, answer, original):
        """A declared valid alternative is distinct from valid execution."""
        result = {"execution": self.executed_action(original),
                  "constraint_validity": {"evaluated": False,
                                          "reason": "Decision membership does not evaluate every constraint"}}
        if self.decision_source is None or original is None:
            return dict(result, declared_alternative={"evaluated": False,
                "reason": "No original Decision reference and recorded probe supplied"})
        observed = original["scoped_facts"]
        if not observed["evaluated"]:
            return dict(result, declared_alternative=observed)
        selected = tuple(item for item in observed["decisions"]
                         if item["lineage"] == self.decision_source)
        if not selected:
            return dict(result, declared_alternative={"evaluated": False,
                "reason": "Referenced Decision is not current in the original captured scope"})
        item, = selected
        decision = item["declaration"]
        if self.expected not in (decision.chosen, *decision.rejected):
            raise ValueError("Frozen action oracle contradicts its original Decision alternatives")
        return dict(result, declared_alternative={
            "evaluated": True, "missing": answer is None,
            "valid": answer in (decision.chosen, *decision.rejected),
            "chosen": answer == decision.chosen, "source": FieldCodec.encode(item["current"]),
            "scope": observed["scope"],
        })

    def executed_action(self, original):
        """Corroborate a named SDK result without crediting a lexical proposal."""
        if self.action_source is None or original is None:
            return {"evaluated": False, "reason": "No original request/result source supplied"}
        selected = tuple(step for step in original["tool_steps"]
                         if step["source"] == self.action_source)
        if len(selected) != 1:
            raise ValueError("Execution source is outside this original probe branch")
        step, = selected
        return dict(step["completion"], source=FieldCodec.encode(step["source"]), call=step["call"],
                    scope="Original SDK tool result, not current filesystem or every task constraint",
                    proposal_alignment={"evaluated": False,
                        "reason": "A tool receipt does not bind its arguments to a declared Decision alternative"})


@dataclass(frozen=True)
class RecallRound:
    identity: str
    history: tuple[str, ...]
    questions: tuple[Question, ...]

    def __post_init__(self) -> None:
        identities = tuple(question.identity for question in self.questions)
        if len(set(identities)) != len(identities):
            raise ValueError("A frozen round requires unique question identities")
        if not self.identity or not self.history or not self.questions:
            raise ValueError("A frozen round requires identity, history and questions")

    def public(self) -> dict:
        return {
            "id": self.identity,
            "history": self.history,
            "questions": [question.public() for question in self.questions],
        }

    def probe_text(self) -> str:
        """Held-out questions, without expected, stale or evidence metadata."""
        return (
            "Answer these recall questions using the supplied history. Return only JSON "
            "mapping the round ID to an object of question IDs and exact answer strings.\n"
            + json.dumps({"round": self.identity,
                          "questions": [question.public() for question in self.questions]})
        )

    def score_native(self, probe: RecordedNativeProbe):
        return self.score_recorded(probe.observe())

    def score_recorded(self, original):
        """Score an already corroborated original; resource ownership stays upstream."""
        if original["prompt"] != self.probe_text():
            raise ValueError("Recorded native probe differs from the frozen held-out questions")
        answers = decode_answers(original["answer_text"])
        if answers.rounds.keys() != {self.identity}:
            raise ValueError("Recorded native reply must answer exactly its original round")
        return self.score(answers.rounds[self.identity]), original

    def score(self, answers: dict[str, str]) -> ScoredRound:
        return ScoredRound(self, dict(answers))


@dataclass(frozen=True)
class RecallScenario:
    identity: str
    rounds: tuple[RecallRound, ...]

    def __post_init__(self) -> None:
        identities = tuple(item.identity for item in self.rounds)
        if len(set(identities)) != len(identities):
            raise ValueError("A frozen scenario requires unique round identities")
        if not self.identity or not self.rounds:
            raise ValueError("A frozen scenario requires identity and rounds")

    @classmethod
    def read(cls, path: Path) -> RecallScenario:
        """Decode an authored frozen oracle once, never from candidate answers."""
        return FieldCodec.decode(cls, json.loads(path.read_text(), object_pairs_hook=unique_fields))

    def public(self) -> dict:
        return {"scenario": self.identity, "rounds": [item.public() for item in self.rounds]}

    def score(self, condition: Condition, answers: RecordedAnswers) -> ScoredScenario:
        unexpected = answers.rounds.keys() - {item.identity for item in self.rounds}
        if unexpected:
            raise ValueError(f"Unknown rounds: {sorted(unexpected)}")
        return ScoredScenario(
            self,
            condition,
            tuple(item.score(answers.rounds.get(item.identity, {})) for item in self.rounds),
        )

    def observe_native(self, condition: Condition, probes: RecordedNativeProbes):
        unexpected = probes.rounds.keys() - {item.identity for item in self.rounds}
        if unexpected:
            raise ValueError(f"Unknown native rounds: {sorted(unexpected)}")
        checkpoints, evidence = probes.observe(self.rounds)
        scored = []
        for item in self.rounds:
            if item.identity in probes.rounds:
                score, _ = item.score_recorded(evidence[item.identity])
            else:
                score = item.score({})
            scored.append(score)
        return ScoredScenario(self, condition, tuple(scored)), checkpoints, evidence

    def score_native(self, condition: Condition, probes: RecordedNativeProbes) -> dict:
        score, checkpoints, evidence = self.observe_native(condition, probes)
        return score.public_native(checkpoints, evidence)

    def compare_native(self, condition, probes, baseline_condition, baseline):
        """The same frozen oracle scores both arms; original records own alignment."""
        if condition == baseline_condition:
            raise ValueError("A comparison requires distinct declared conditions")
        score, cuts, original = self.observe_native(condition, probes)
        control, baseline_cuts, baseline_original = self.observe_native(baseline_condition, baseline)
        alignment = probes.alignment(baseline, original, baseline_original, self.rounds)
        return {"candidate": score.public_native(cuts, original),
                "baseline": control.public_native(baseline_cuts, baseline_original),
                "alignment": alignment,
                "paired_quality": score.paired_quality(control, original, baseline_original, alignment),
                "condition_construction": {"evaluated": False,
                    "reason": "Condition-specific construction/complete-history eligibility is not supplied by labels"},
                "study_acceptance": {"evaluated": False,
                    "reason": "One recorded sample is not a registered comparative study or margin result"}}



def coding_scenario() -> RecallScenario:
    """Truth comes from authored fixture events, never a candidate summary."""
    first = (
        "User: Work on synthetic goal goal-alpha. Keep the unresolved failure fail-17 open.",
        "Source receipt src@r1: Symbol PacketOwner lives in src/demo/packet.py at commit abc123.",
        "User correction c1: Export root is /data/run-a. Never expose a wildcard listener.",
        "Input ledger i1: Pending input input-9 has disposition UNKNOWN; do not replay it.",
        "Tool call t1: inspect src/demo/packet.py. Tool result t1: read completed.",
        "Assistant: Completed the source inspection subtask; implementation remains unfinished.",
    )
    second = first + (
        "User correction c2 supersedes c1: Export root is /data/run-b, not /data/run-a.",
        "Source receipt src@r2 supersedes r1: Rename PacketOwner to FrameOwner in src/demo/frame.py.",
        "Assistant: The implementation is unfinished. fail-17 remains unresolved; input-9 remains UNKNOWN.",
    )
    third = second + (
        "User decision g2: Replace goal-alpha with goal-beta. Old goal must not be resurrected.",
        "Source receipt src@r3: Current commit is def456; FrameOwner remains in src/demo/frame.py.",
        "Assistant: Inspection subtask completed. No resolution for fail-17 or input-9 was received.",
    )

    def questions(symbol, path, root, goal, commit, source, correction, obsolete):
        return (
            Question(
                "symbol", "What is the current exact symbol name?", symbol, source, obsolete[0]
            ),
            Question(
                "path",
                "What is its current repository-relative file path?",
                path,
                source,
                obsolete[1],
            ),
            Question(
                "root",
                "What export root did the latest user correction specify?",
                root,
                correction,
                obsolete[2],
            ),
            Question(
                "goal", "What is the current exact goal identity?", goal, "goal ledger", obsolete[3]
            ),
            Question(
                "commit", "What is the current exact source commit?", commit, source, obsolete[4]
            ),
            Question(
                "failure",
                "Which unresolved failure identity must remain open?",
                "fail-17",
                "failure ledger",
            ),
            Question(
                "input",
                "What is input-9's exact disposition?",
                "UNKNOWN",
                "input ledger i1",
                ("STARTED", "COMPLETED"),
                Measurement.PROHIBITION,
            ),
        )

    return RecallScenario(
        "coding-corrections-v1",
        (
            RecallRound(
                "r1",
                first,
                questions(
                    "PacketOwner",
                    "src/demo/packet.py",
                    "/data/run-a",
                    "goal-alpha",
                    "abc123",
                    "src@r1",
                    "c1",
                    ((), (), (), (), ()),
                ),
            ),
            RecallRound(
                "r2",
                second,
                questions(
                    "FrameOwner",
                    "src/demo/frame.py",
                    "/data/run-b",
                    "goal-alpha",
                    "abc123",
                    "src@r2",
                    "c2",
                    (("PacketOwner",), ("src/demo/packet.py",), ("/data/run-a",), (), ()),
                ),
            ),
            RecallRound(
                "r3",
                third,
                questions(
                    "FrameOwner",
                    "src/demo/frame.py",
                    "/data/run-b",
                    "goal-beta",
                    "def456",
                    "src@r3",
                    "c2",
                    (
                        ("PacketOwner",),
                        ("src/demo/packet.py",),
                        ("/data/run-a",),
                        ("goal-alpha",),
                        ("abc123",),
                    ),
                ),
            ),
        ),
    )


def decode_answers(text: str) -> RecordedAnswers:
    return FieldCodec.decode(
        RecordedAnswers, {"rounds": json.loads(text, object_pairs_hook=unique_fields)}
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario-file", type=Path,
                        help="Frozen authored RecallScenario oracle; never sent as provider JSON")
    recorded = parser.add_mutually_exclusive_group()
    recorded.add_argument("--answers", type=Path)
    recorded.add_argument("--native-probes", type=Path)
    recorded.add_argument("--recorded-run", type=Path,
                          help="RecordedNativeProbes with ordered checkpoint and original evidence references")
    parser.add_argument("--compare-recorded-run", type=Path,
                        help="Independent original control; requires --recorded-run")
    parser.add_argument("--baseline-condition", type=Condition, choices=tuple(Condition),
                        default=Condition.BOUNDED)
    recorded.add_argument("--native-checkpoint", type=Path,
                          help="RecordedNativeCheckpoint reference to an original managed cut")
    parser.add_argument("--previous-checkpoint", type=Path,
                        help="Original ancestor cut; reports source changes, not revision authority")
    recorded.add_argument("--probe-prompts", action="store_true")
    parser.add_argument(
        "--condition", type=Condition, choices=tuple(Condition), default=Condition.BOUNDED
    )
    args = parser.parse_args()
    if args.previous_checkpoint is not None and args.native_checkpoint is None:
        parser.error("--previous-checkpoint requires --native-checkpoint")
    if args.compare_recorded_run is not None and args.recorded_run is None:
        parser.error("--compare-recorded-run requires --recorded-run")
    scenario = RecallScenario.read(args.scenario_file) if args.scenario_file else coding_scenario()
    result = scenario.public()
    if args.probe_prompts:
        result = {item.identity: item.probe_text() for item in scenario.rounds}
    if args.answers is not None:
        result = scenario.score(args.condition, decode_answers(args.answers.read_text())).public()
    if args.native_probes is not None:
        probes = FieldCodec.decode(RecordedNativeProbes, {
            "rounds": json.loads(args.native_probes.read_text(), object_pairs_hook=unique_fields)
        })
        result = scenario.score_native(args.condition, probes)
    if args.recorded_run is not None:
        probes = FieldCodec.decode(RecordedNativeProbes, json.loads(
            args.recorded_run.read_text(), object_pairs_hook=unique_fields
        ))
        if args.compare_recorded_run is None:
            result = scenario.score_native(args.condition, probes)
        else:
            baseline = FieldCodec.decode(RecordedNativeProbes, json.loads(
                args.compare_recorded_run.read_text(), object_pairs_hook=unique_fields))
            result = scenario.compare_native(args.condition, probes, args.baseline_condition, baseline)
    if args.native_checkpoint is not None:
        checkpoint = FieldCodec.decode(RecordedNativeCheckpoint, json.loads(
            args.native_checkpoint.read_text(), object_pairs_hook=unique_fields
        ))
        previous = FieldCodec.decode(RecordedNativeCheckpoint, json.loads(
            args.previous_checkpoint.read_text(), object_pairs_hook=unique_fields
        )) if args.previous_checkpoint is not None else None
        result = checkpoint.inspect(previous)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
