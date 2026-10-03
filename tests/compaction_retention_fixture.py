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
from retained_native_fixture import RecordedNativeCheckpoint, RecordedNativeProbe


@dataclass(frozen=True)
class RecordedAnswers:
    rounds: dict[str, dict[str, str]]


@dataclass(frozen=True)
class RecordedNativeProbes:
    rounds: dict[str, RecordedNativeProbe]
    checkpoints: dict[str, RecordedNativeCheckpoint] = field(default_factory=dict)

    def observe(self, rounds):
        """Visit declared cuts in frozen round order, borrowing each source once."""
        selected = dict(self.checkpoints)
        for identity, probe in self.rounds.items():
            if probe.checkpoint is not None:
                if identity in selected and selected[identity] != probe.checkpoint:
                    raise ValueError("Round declares different probe and measurement checkpoints")
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
    answers: tuple[tuple[Question, AnswerScore], ...]

    @property
    def identity(self) -> str:
        return self.source.identity

    @property
    def scored_answers(self) -> Iterator[tuple[Question, AnswerScore]]:
        return iter(self.answers)

    def public(self) -> dict:
        return {
            "round": self.identity,
            **self.public_totals(),
            "measurements": self.measurement_totals(),
            "answers": {question.identity: asdict(outcome) for question, outcome in self.answers},
        }


@dataclass(frozen=True)
class ScoredScenario(ScoreView):
    source: RecallScenario
    condition: Condition
    rounds: tuple[ScoredRound, ...]

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
            "synthetic": True,
            "rounds": [item.public() for item in self.rounds],
            **self.public_totals(),
            "measurements": self.measurement_totals(),
        }


@dataclass(frozen=True)
class Question:
    identity: str
    prompt: str
    expected: str
    evidence_ref: str
    obsolete: tuple[str, ...] = ()
    measurement: Measurement = Measurement.RECALL

    def __post_init__(self) -> None:
        if not self.identity or not self.prompt or not self.evidence_ref:
            raise ValueError("A frozen question requires identity, prompt and original evidence")
        if self.expected in self.obsolete:
            raise ValueError("A current expected answer cannot also be obsolete")

    def score(self, answer: str | None) -> AnswerScore:
        return AnswerScore(
            correct=answer == self.expected,
            stale=answer in self.obsolete,
            missing=answer is None,
        )

    def public(self) -> dict:
        return {"id": self.identity, "prompt": self.prompt}


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
        unexpected = answers.keys() - {question.identity for question in self.questions}
        if unexpected:
            raise ValueError(f"Unknown questions in {self.identity}: {sorted(unexpected)}")
        return ScoredRound(
            self,
            tuple(
                (question, question.score(answers.get(question.identity)))
                for question in self.questions
            ),
        )


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

    def score_native(self, condition: Condition, probes: RecordedNativeProbes) -> dict:
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
        result = ScoredScenario(self, condition, tuple(scored)).public()
        return dict(result, native_probes=evidence,
                    scope="recorded original native probes; condition label is not construction proof",
                    checkpoints=checkpoints,
                    original_checkpoint_count=len(checkpoints),
                    three_original_cuts_observed=len(checkpoints) >= 3,
                    canonical_availability={
                        item.identity: checkpoints[item.identity]["canonical_availability"]
                        if item.identity in checkpoints else {
                            "evaluated": False, "reason": "No original committed checkpoint supplied"
                        } for item in self.rounds
                    },
                    provider_prompt_presence={
                        item.identity: evidence[item.identity]["provider_prompt_presence"]
                        if item.identity in evidence else {
                            "evaluated": False, "reason": "No original native probe supplied"
                        } for item in self.rounds
                    },
                    revision_mass={identity: report["revision_mass"]
                                   for identity, report in checkpoints.items()},
                    recall_scope="Original recorded answers only; authored answers are scorer controls")


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
        result = scenario.score_native(args.condition, probes)
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
