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
from math import ceil, floor, isfinite
from pathlib import Path
from random import Random
from statistics import mean

from agent_comms.field_codec import FieldCodec
from agent_comms.pi_rpc import unique_fields
from agent_comms.native_entries import NativeEntry
from agent_comms.native_session_reopen import NativeSessionIdentity
from agent_comms.native_tools import CodingTool
from agent_comms.message_reference import MessageReference
from agent_comms.turn_context import FileProvenance, JournalProvenance, ToolCatalogSegment
from retained_native_fixture import RecordedNativeCheckpoint, RecordedNativeProbe


@dataclass(frozen=True)
class RecordedAnswers:
    rounds: dict[str, dict[str, str]]


@dataclass(frozen=True)
class PairedRecallDesign:
    """Supplied analysis parameters, not preregistration or spending authority.

    The original oracle file owns questions and round membership. Original
    native admission owns model selection. This value owns the supplied
    construction, comparison and inference parameters; it cannot reconstruct
    missing facts or approve execution.
    """
    oracle: FileProvenance
    candidate: Condition
    baseline: Condition
    model: str
    sample_count: int
    confidence: float
    recall_margin: float
    bootstrap_samples: int
    bootstrap_seed: int

    def __post_init__(self):
        if self.candidate == self.baseline:
            raise ValueError("A comparison design requires distinct conditions")
        if not self.model or self.sample_count < 2:
            raise ValueError("A comparison design requires a model and at least two trajectories")
        if not isfinite(self.confidence) or not 0 < self.confidence < 1:
            raise ValueError("Confidence must be finite and strictly between zero and one")
        if not isfinite(self.recall_margin) or not -1 <= self.recall_margin <= 1:
            raise ValueError("Recall margin must be finite and between minus one and one")
        if self.bootstrap_samples < ceil(2 / (1 - self.confidence)):
            raise ValueError("Bootstrap samples must represent both requested interval tails")

    def compare(self, pairs):
        """Read supplied originals through the existing comparison algorithm."""
        if len(pairs) != self.sample_count:
            raise ValueError("Recorded pair count differs from the supplied design")
        scenario = RecordedNativeCheckpoint.read_record(self.oracle, RecallScenario)
        result = scenario.compare_native_pairs(self.candidate, pairs, self.baseline)
        result['comparison_design'] = FieldCodec.encode(self)
        result['recall_inference'] = ScoredScenario.paired_inference(result['pairs'], self)
        return result

    def construction_plan(self, sampling_seed: int):
        """Export prospective operands; never grant or launch a native turn.

        The pinned oracle owns source additions and held-out questions. The
        sampling seed owns this prospective ordering, independently of the
        bootstrap seed used later for analysis. Recorded inputs must still
        corroborate what was actually executed; this plan cannot supply them.
        """
        scenario = RecordedNativeCheckpoint.read_record(self.oracle, RecallScenario)
        rounds = scenario.construction_rounds()
        random = Random(sampling_seed)
        return {'comparison_design': self, 'scenario': scenario.identity,
                'rounds': rounds, 'sampling_seed': sampling_seed,
                'trajectories': tuple({'sample': index + 1,
                    'condition_order': tuple(random.sample((self.candidate, self.baseline), 2))}
                    for index in range(self.sample_count)),
                'scope': 'Prospective authored source/probe operands and randomized arm order only; '
                         'no native input, checkpoint, intervention, registration, capacity or spending grant'}


@dataclass(frozen=True)
class RecordedNativeProbes:
    rounds: dict[str, RecordedNativeProbe]
    checkpoints: dict[str, RecordedNativeCheckpoint] = field(default_factory=dict)

    def __post_init__(self):
        originals = tuple((probe.session, probe.input_id) for probe in self.rounds.values())
        if len(set(originals)) != len(originals):
            raise ValueError("A recorded trajectory cannot count the same original input twice")

    @staticmethod
    def require_distinct(runs):
        """Distinct recorded trajectories cannot reuse a probe journal/input.

        This prevents duplicate sampling; it does not establish statistical
        independence, randomization or a registered experimental design.
        Original cut parents may be shared for a same-cut quality comparison.
        """
        sessions, inputs = set(), set()
        for run in runs:
            selected = {probe.session for probe in run.rounds.values()}
            selected_inputs = {probe.input_id for probe in run.rounds.values()}
            if sessions & selected or inputs & selected_inputs:
                raise ValueError("A comparison batch cannot reuse original probe sessions or inputs")
            sessions.update(selected)
            inputs.update(selected_inputs)

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
                _, entry, _, _ = cut.capture(session, evidence)
                terminals.append(entry)
            for probe in self.rounds.values():
                session.require_same_session(probe.session)
                probe.read(evidence, evidence)
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

    def checkpoints_for(self, rounds):
        """Select this trajectory's original cuts against the complete oracle."""
        identities = {item.identity for item in rounds}
        unexpected = self.rounds.keys() - identities
        if unexpected:
            raise ValueError(f"Unknown native rounds: {sorted(unexpected)}")
        if self.rounds.keys() & self.checkpoints.keys():
            raise ValueError("A probed round's checkpoint belongs on its RecordedNativeProbe")
        selected = dict(self.checkpoints)
        for identity, probe in self.rounds.items():
            if probe.checkpoint is not None:
                selected[identity] = probe.checkpoint
        unexpected = selected.keys() - identities
        if unexpected:
            raise ValueError(f"Unknown checkpoint rounds: {sorted(unexpected)}")
        return selected

    @staticmethod
    def observe_runs(runs, rounds):
        """Borrow originals for one bounded observation, then release all readers.

        A pair shares its parent descriptor/decoded bytes, never a cached proof.
        Each arm still observes and corroborates its own original records.
        Batch comparison calls this once per pair, not for the entire study.
        """
        runs = tuple(runs)
        selected = tuple(run.checkpoints_for(rounds) for run in runs)
        with RecordedNativeProbe.original_readers(
                chain.from_iterable(run.rounds.values() for run in runs),
                chain.from_iterable(cuts.values() for cuts in selected)) as sources:
            return tuple(run.observe_acquired(rounds, sources, cuts)
                         for run, cuts in zip(runs, selected))

    def observe(self, rounds):
        """Visit one trajectory using the same acquired-reader algorithm."""
        observed, = self.observe_runs((self,), rounds)
        return observed

    def observe_acquired(self, rounds, sources, selected):
        cuts = tuple((item.identity, selected[item.identity]) for item in rounds
                     if item.identity in selected)
        reports, observations = {}, {}
        previous = None
        for identity, checkpoint in cuts:
            evidence = sources[Path(checkpoint.reference.session_file)]
            header, _ = evidence.observe()
            session = NativeSessionIdentity(header.id, str(evidence.source.path))
            attempt, entry, covered, assembly = checkpoint.capture(session, evidence)
            report = checkpoint._report(attempt, entry, covered, assembly)
            if previous is not None:
                old, prior_attempt, prior_entry, prior_session = previous
                prior_session.require_same_session(session)
                if prior_entry.id == entry.id or prior_entry not in evidence.branch(entry.id, evidence.entries):
                    raise ValueError("Repeated measurements require distinct original ancestor cuts")
                report["source_changes"] = attempt.request.retained.changed_from(prior_attempt.request.retained)
                report["revision_mass"] = checkpoint.revision_from(old, prior_attempt, attempt)
            reports[identity] = report
            previous = checkpoint, attempt, entry, session
        for identity, probe in self.rounds.items():
            observations[identity] = probe.read(sources[Path(probe.session.session_file)],
                                                sources[probe.checkpoint_source])
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
            configured = {name: self.same_observations(f"captured model/effort ({name})", tuple(
                (item[name],) if item["evaluated"] else (None,) for item in settings))
                for name in ("model", "thinking")}
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
            request = self.request_alignment(original, control)
            completion = self.completion_alignment(original, control)
            terminals = a["request_completion"], b["request_completion"]
            unavailable = tuple(item["reason"] for item in (request, completion, *terminals)
                                if not item["evaluated"])
            pairs[identity] = {
                "evaluated": not unavailable,
                "scope": "Common original SDK fork source, admitted request models joined to original SDK terminals, tool catalog and frozen probe; captured settings are independent, not complete intervention/construction proof",
                "captured_settings": configured,
                "request_selection": request,
                "completion_selection": completion,
                "request_completion": terminals,
                "sdk_manifest_changes": a["sdk_manifest"].changed_since(b["sdk_manifest"]),
                "reason": "; ".join(unavailable) if unavailable else
                          "Original request and completion selections corroborate the source match",
            }
        return pairs

    @staticmethod
    def request_alignment(original, control):
        """Compare the probes' exact admitted models, never infer from config.

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
        observed = tuple(point.model for group in points for point in group)
        if any(model is None or model.display_name is None for model in observed):
            return {"evaluated": False, "reason": "Original admitted request model unavailable"}
        selection = points[1][0].model.display_name
        models = []
        for group in points:
            selected = []
            for point in group:
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
        return {"evaluated": True, "reason": "Original admitted request models match each other",
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
        return self.by_measurement(self.scored_answers)

    @classmethod
    def by_measurement(cls, scored_answers) -> dict:
        grouped = {}
        for question, outcome in scored_answers:
            grouped.setdefault(question.measurement, []).append(outcome)
        return {measurement.value: cls.totals(iter(outcomes))
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
                    recorded_resources=self.recorded_resources(checkpoints, evidence),
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

    def recorded_resources(self, checkpoints, evidence):
        """Total acquired original completions, never estimates or billing.

        A missing usage/counter leaves that metric unavailable. Summary and
        assistant records remain separate so shared cuts cannot be hidden in a
        paired cost comparison. Frozen rounds own completeness, not the subset
        of supplied records. Output already includes reported reasoning;
        neither tokens nor cost is reconstructed from component counters.
        """
        rounds = tuple(round_.identity for round_ in self.source.rounds)
        summaries = {identity: (report['summary_usage']['usage']
            if report['summary_usage']['evaluated'] else None,)
            for identity, report in checkpoints.items()}
        assistants = {identity: tuple(step['usage']['value']
            for step in original['model_steps']) for identity, original in evidence.items()}

        def metric(values, expected, complete_rounds):
            supplied = tuple(value for value in values if value is not None)
            complete = complete_rounds and expected > 0 and len(supplied) == expected
            return {'evaluated': complete, 'value': sum(supplied) if complete else None,
                    'observed_records': len(supplied),
                    'expected_records': expected if complete_rounds else None,
                    'observed_value': sum(supplied) if supplied else None}

        def total(*groups):
            records = tuple(chain.from_iterable(records for group in groups for records in group.values()))
            observed_rounds = tuple(identity for identity in rounds
                                    if all(group.get(identity) for group in groups))
            missing_rounds = tuple(identity for identity in rounds if identity not in observed_rounds)
            def measured(values):
                return metric(values, len(records), not missing_rounds)

            available = tuple(record for record in records if record is not None)
            return {'records': len(records),
                'usage_records': len(available),
                'expected_rounds': rounds, 'observed_rounds': observed_rounds,
                'missing_rounds': missing_rounds,
                'input_tokens': measured(usage.input for usage in available),
                'output_tokens': measured(usage.output for usage in available),
                'cache_read_tokens': measured(usage.cache_read for usage in available),
                'cache_write_tokens': measured(usage.cache_write for usage in available),
                'reported_total_tokens': measured(usage.total_tokens for usage in available),
                'reasoning_tokens': measured(usage.reasoning for usage in available),
                'normalized_cost': measured(usage.cost.total for usage in available
                                           if usage.cost is not None)}

        return {'summaries': total(summaries), 'assistants': total(assistants),
                'combined': total(summaries, assistants),
                'scope': 'Original journaled summary and assistant completions only; '
                         'complete totals require observations for every frozen round; '
                         'observed subtotals do not estimate missing work or prove no summary work; '
                         'SDK-normalized cost is not billed spend; no unjournaled retries, '
                         'cache-saving comparison, HTTP accounting or end-to-end timing'}

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
            measurements = tuple(self.by_measurement(chain.from_iterable(
                round_.scored_answers for round_ in selected)) for selected in (
                    rounds, tuple(controls[round_.identity] for round_ in rounds)))
            recall = tuple(items.get(Measurement.RECALL.value) for items in measurements)
            recall_evaluated = evaluated and all(item is not None and item['questions'] for item in recall)
            measured[name] = {"candidate": candidate, "baseline": control,
                              "evaluated": evaluated,
                              "measurements": {"candidate": measurements[0], "baseline": measurements[1]},
                              "recall_rate_difference": {
                                  "evaluated": recall_evaluated,
                                  "value": recall[0]['correct'] / recall[0]['questions']
                                           - recall[1]['correct'] / recall[1]['questions']
                                           if recall_evaluated else None},
                              "correct_difference": candidate["correct"] - control["correct"]
                                                    if evaluated else None,
                              "scope": "Descriptive differences for matched original source and selections; not certified interventions or study margins"}
        return measured

    @staticmethod
    def paired_batch(comparisons):
        """Keep one rate observation per recorded trajectory, never per cut.

        Matched-source descriptive quality remains distinct from verified
        interventions and registered study inference. Missing/assisted samples
        stay visible; unavailable recall is never an observed zero.
        """
        groups = {}
        for name in comparisons[0]['paired_quality']:
            samples = tuple(pair['paired_quality'][name] for pair in comparisons)
            rates = tuple(sample['recall_rate_difference'] for sample in samples)
            values = tuple(rate['value'] for rate in rates if rate['evaluated'])
            groups[name] = {
                'trajectories': len(samples), 'recall_trajectories': len(values),
                'samples': samples,
                'mean_recall_rate_difference': {
                    'evaluated': bool(values) and len(values) == len(samples),
                    'value': mean(values) if values and len(values) == len(samples) else None},
                'scope': 'One descriptive recall rate per recorded trajectory; '
                         'no confidence interval, registered margin, intervention or independence grant'}
        return groups

    @staticmethod
    def paired_inference(comparisons, design: PairedRecallDesign):
        """One sample is a complete unassisted trajectory, not a source cut.

        This conditional paired bootstrap describes supplied records. Neither
        a file nor an interval proves preregistration, randomization, independent
        sampling, correct interventions, billed cost or end-to-end acceptance.
        Missing and assisted recall cannot be dropped to improve the interval.
        """
        if len(comparisons) != design.sample_count:
            raise ValueError("Inference pair count differs from the supplied design")
        samples, unavailable = [], []
        for index, pair in enumerate(comparisons):
            for identity, alignment in pair['alignment'].items():
                if not alignment['evaluated']:
                    unavailable.append(f"Pair {index + 1}, round {identity}: original alignment unavailable")
                    continue
                # request_alignment already requires all admitted observations
                # in both arms to match. Bind that selection to the supplied
                # model through the same PiModel owner, never registry settings.
                original = alignment['request_selection']['candidate']['observations'][0]
                original.model.require_selection(design.model)
            quality = pair['paired_quality']['unassisted_recall']
            frozen = pair['candidate']['measurements'].get(Measurement.RECALL.value)
            measured = quality['measurements']['candidate'].get(Measurement.RECALL.value)
            rate = quality['recall_rate_difference']
            if (not frozen or not frozen['questions'] or not measured
                    or measured['questions'] != frozen['questions'] or not rate['evaluated']):
                unavailable.append(f"Pair {index + 1}: complete unassisted recall unavailable")
            else:
                samples.append(rate['value'])
        result = {
            'evaluated': not unavailable, 'expected_trajectories': design.sample_count,
            'observed_trajectories': len(samples), 'reasons': unavailable,
            'method': 'Paired trajectory percentile bootstrap with outward empirical order statistics',
            'confidence': design.confidence, 'margin': design.recall_margin,
            'bootstrap_samples': design.bootstrap_samples, 'bootstrap_seed': design.bootstrap_seed,
            'scope': 'Conditional recall inference on supplied complete trajectories; '
                     'no registration, independence, intervention, cost, timing or study acceptance grant',
        }
        if unavailable:
            return result
        random = Random(design.bootstrap_seed)
        resampled = sorted(mean(random.choices(samples, k=len(samples)))
                           for _ in range(design.bootstrap_samples))
        tail = (1 - design.confidence) / 2
        lower = resampled[floor(tail * (len(resampled) - 1))]
        upper = resampled[ceil((1 - tail) * (len(resampled) - 1))]
        return dict(result, mean_recall_rate_difference=mean(samples),
                    lower=lower, upper=upper, meets_recall_margin=lower >= design.recall_margin)


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
    # Frozen oracle intent, never reconstructed from a model answer or receipt.
    # CodingTool owns the native invocation; no second argument schema lives here.
    expected_tool: CodingTool | None = None

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
        if self.expected_tool is not None and self.measurement is not Measurement.ACTION:
            raise ValueError("A frozen intended invocation belongs on an action question")

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
        return dict(result, declared_alternative=self.declared_alternative(answer, original))

    def declared_alternative(self, answer, original):
        """Resolve the original scoped Decision once for proposal and execution."""
        if self.decision_source is None or original is None:
            return {"evaluated": False,
                "reason": "No original Decision reference and recorded probe supplied"}
        observed = original["scoped_facts"]
        if not observed["evaluated"]:
            return observed
        selected = tuple(item for item in observed["decisions"]
                         if item["lineage"] == self.decision_source)
        if not selected:
            return {"evaluated": False,
                "reason": "Referenced Decision is not current in the original captured scope"}
        item, = selected
        decision = item["declaration"]
        if not decision.contains_alternative(self.expected):
            raise ValueError("Frozen action oracle contradicts its original Decision alternatives")
        return {
            "evaluated": True, "missing": answer is None,
            "valid": decision.contains_alternative(answer),
            "chosen": answer == decision.chosen, "source": item["current"],
            "scope": observed["scope"],
        }

    def executed_action(self, original):
        """Corroborate a named SDK result without crediting a lexical proposal."""
        if self.action_source is None or original is None:
            return {"evaluated": False, "reason": "No original request/result source supplied"}
        selected = tuple(step for step in original["tool_steps"]
                         if step["source"] == self.action_source)
        if len(selected) != 1:
            raise ValueError("Execution source is outside this original probe branch")
        step, = selected
        return dict(step["completion"], source=step["source"], call=step["call"],
                    scope="Original SDK tool result, not current filesystem or every task constraint",
                    proposal_alignment=self.bind_execution(step["call"], original))

    def bind_execution(self, request, original):
        """Match frozen invocation intent to an original scoped alternative.

        This is oracle-to-request alignment. A successful result and permission
        under prose constraints remain separate questions; neither is inferred.
        """
        if self.expected_tool is None:
            return {"evaluated": False, "reason": "No frozen intended tool invocation supplied"}
        alternative = self.declared_alternative(self.expected, original)
        if not alternative["evaluated"]:
            return alternative
        return {"evaluated": True,
                "matches": self.expected_tool.matches_request(request),
                "alternative": self.expected, "decision_source": alternative["source"],
                "expected_tool": self.expected_tool,
                "scope": "Frozen oracle invocation and current original Decision; not task constraint permission"}


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

    def history_after(self, previous: tuple[str, ...]) -> tuple[str, ...]:
        """Return only new authored history; never silently rebuild a branch."""
        if self.history[:len(previous)] != previous:
            raise ValueError("Construction requires the exact preceding frozen history prefix")
        return self.history[len(previous):]

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

    def construction_rounds(self):
        """Derive ordered source additions and public probes from one oracle.

        Native execution still owns checkpoint creation and input admission.
        These operands do not repeat cumulative source at each cut or disclose
        scoring metadata as provider instructions.
        """
        previous, rounds = (), []
        for round_ in self.rounds:
            rounds.append({'round': round_.identity,
                           'history_additions': round_.history_after(previous),
                           'probe_text': round_.probe_text()})
            previous = round_.history
        return tuple(rounds)

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
        return self.score_observed(condition, probes.observe(self.rounds))

    def score_observed(self, condition, observed):
        """Score acquired original observations with the frozen oracle once."""
        checkpoints, evidence = observed
        scored = []
        for item in self.rounds:
            if item.identity in evidence:
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
        observed, baseline_observed = RecordedNativeProbes.observe_runs((probes, baseline), self.rounds)
        score, cuts, original = self.score_observed(condition, observed)
        control, baseline_cuts, baseline_original = self.score_observed(baseline_condition, baseline_observed)
        alignment = probes.alignment(baseline, original, baseline_original, self.rounds)
        return {"candidate": score.public_native(cuts, original),
                "baseline": control.public_native(baseline_cuts, baseline_original),
                "alignment": alignment,
                "paired_quality": score.paired_quality(control, original, baseline_original, alignment),
                "condition_construction": {"evaluated": False,
                    "reason": "Condition-specific construction/complete-history eligibility is not supplied by labels"},
                "study_acceptance": {"evaluated": False,
                    "reason": "One recorded sample is not a registered comparative study or margin result"}}

    def compare_native_pairs(self, condition, pairs, baseline_condition):
        """Read a batch of original pairs through the same frozen scorer.

        This is a recorded-data runner, never a provider launcher. A supplied
        batch is not a preregistration or permission to run an experiment.
        """
        if not pairs:
            raise ValueError("A recorded comparison batch requires at least one pair")
        RecordedNativeProbes.require_distinct(chain.from_iterable(pairs))
        comparisons = tuple(self.compare_native(condition, candidate, baseline_condition, control)
                            for candidate, control in pairs)
        return {'scenario': self.identity, 'pairs': comparisons,
                'paired_quality': ScoredScenario.paired_batch(comparisons),
                'study_acceptance': {'evaluated': False,
                    'reason': 'Recorded pairs do not supply a registered design, verified '
                              'condition construction or complete cost/end-to-end journeys'}}



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
    recorded.add_argument("--recorded-pairs", type=Path,
                          help="Array of candidate/control RecordedNativeProbes pairs; reads originals only, no study launch")
    recorded.add_argument("--construction-plan", action="store_true",
                          help="Export prospective original-oracle history/probe operands and arm order; no native/model launch")
    parser.add_argument("--compare-recorded-run", type=Path,
                        help="Independent original control; requires --recorded-run")
    parser.add_argument("--baseline-condition", type=Condition, choices=tuple(Condition),
                        help="Control label for an unplanned recorded comparison")
    parser.add_argument("--comparison-design", type=Path,
                        help="PairedRecallDesign for --recorded-pairs or --construction-plan; owns oracle, conditions and analysis parameters")
    parser.add_argument("--sampling-seed", type=int,
                        help="Explicit prospective arm-order seed for --construction-plan; distinct from bootstrap seed")
    recorded.add_argument("--native-checkpoint", type=Path,
                          help="RecordedNativeCheckpoint reference to an original managed cut")
    parser.add_argument("--fork-journal", type=Path,
                        help="PRIVATE original NativeForkCreation journal for condition source")
    parser.add_argument("--fork-session", type=Path,
                        help="Original SDK-created child selected for condition construction")
    parser.add_argument("--condition-source", action="store_true",
                        help="PRIVATE: export original narrative bytes for SDK preview; requires --native-checkpoint")
    parser.add_argument("--previous-checkpoint", type=Path,
                        help="Original ancestor cut; reports source changes, not revision authority")
    recorded.add_argument("--probe-prompts", action="store_true")
    parser.add_argument(
        "--condition", type=Condition, choices=tuple(Condition)
    )
    args = parser.parse_args()
    if (args.fork_journal is None) != (args.fork_session is None):
        parser.error("--fork-journal and --fork-session belong to one SDK creation")
    if args.fork_session is not None and not args.condition_source:
        parser.error("--fork-session requires --condition-source")
    if args.condition_source and args.native_checkpoint is None:
        parser.error("--condition-source requires --native-checkpoint")
    if args.condition_source and args.previous_checkpoint is not None:
        parser.error("--condition-source constructs one checkpoint, not a comparison")
    if args.previous_checkpoint is not None and args.native_checkpoint is None:
        parser.error("--previous-checkpoint requires --native-checkpoint")
    if args.compare_recorded_run is not None and args.recorded_run is None:
        parser.error("--compare-recorded-run requires --recorded-run")
    if args.comparison_design is not None:
        if args.recorded_pairs is None and not args.construction_plan:
            parser.error("--comparison-design requires --recorded-pairs or --construction-plan")
        if any(value is not None for value in (args.scenario_file, args.condition, args.baseline_condition)):
            parser.error("The comparison design owns its oracle and condition labels")
        design = FieldCodec.decode(PairedRecallDesign, json.loads(
            args.comparison_design.read_text(), object_pairs_hook=unique_fields))
    if args.construction_plan:
        if args.comparison_design is None or args.sampling_seed is None:
            parser.error("--construction-plan requires --comparison-design and --sampling-seed")
    elif args.sampling_seed is not None:
        parser.error("--sampling-seed requires --construction-plan")
    condition = args.condition or Condition.BOUNDED
    baseline_condition = args.baseline_condition or Condition.BOUNDED
    scenario = RecallScenario.read(args.scenario_file) if args.scenario_file else coding_scenario()
    result = scenario.public()
    if args.probe_prompts:
        result = {item.identity: item.probe_text() for item in scenario.rounds}
    if args.answers is not None:
        result = scenario.score(condition, decode_answers(args.answers.read_text())).public()
    if args.native_probes is not None:
        probes = FieldCodec.decode(RecordedNativeProbes, {
            "rounds": json.loads(args.native_probes.read_text(), object_pairs_hook=unique_fields)
        })
        result = scenario.score_native(condition, probes)
    if args.recorded_run is not None:
        probes = FieldCodec.decode(RecordedNativeProbes, json.loads(
            args.recorded_run.read_text(), object_pairs_hook=unique_fields
        ))
        if args.compare_recorded_run is None:
            result = scenario.score_native(condition, probes)
        else:
            baseline = FieldCodec.decode(RecordedNativeProbes, json.loads(
                args.compare_recorded_run.read_text(), object_pairs_hook=unique_fields))
            result = scenario.compare_native(condition, probes, baseline_condition, baseline)
    if args.recorded_pairs is not None:
        pairs = FieldCodec.decode(tuple[tuple[RecordedNativeProbes, RecordedNativeProbes], ...],
            json.loads(args.recorded_pairs.read_text(), object_pairs_hook=unique_fields))
        if args.comparison_design is None:
            result = scenario.compare_native_pairs(condition, pairs, baseline_condition)
        else:
            result = design.compare(pairs)
    if args.construction_plan:
        result = design.construction_plan(args.sampling_seed)
    if args.native_checkpoint is not None:
        checkpoint = FieldCodec.decode(RecordedNativeCheckpoint, json.loads(
            args.native_checkpoint.read_text(), object_pairs_hook=unique_fields
        ))
        previous = FieldCodec.decode(RecordedNativeCheckpoint, json.loads(
            args.previous_checkpoint.read_text(), object_pairs_hook=unique_fields
        )) if args.previous_checkpoint is not None else None
        if args.condition_source:
            if args.fork_session is not None:
                result = checkpoint.fork_condition_source(args.fork_journal, args.fork_session)
            else:
                with checkpoint.original_source() as (session, evidence):
                    result = checkpoint.condition_source(session, evidence)
        else:
            result = checkpoint.inspect(previous)
    print(json.dumps(FieldCodec.encode(result), indent=2))


if __name__ == "__main__":
    main()
