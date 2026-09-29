"""Synthetic repeated-compaction recall oracle. No provider or runtime mutation.

Run directly to export public history/questions, or use --answers FILE to score
one condition's recorded answers. Oracle answers never appear in exported inputs.
Exact-match scoring deliberately measures identifiers/state, not prose quality.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from enum import Enum
import json
from pathlib import Path


class Condition(str, Enum):
    """Value-only experimental labels, not production strategy selection."""

    FULL_CONTEXT = "full-context"
    BOUNDED = "bounded"
    TASK_MEMORY = "task-memory"
    RECENT_ONLY = "recent-only"


@dataclass(frozen=True)
class AnswerScore:
    correct: bool
    stale: bool
    missing: bool


@dataclass(frozen=True)
class Question:
    identity: str
    prompt: str
    expected: str
    evidence_ref: str
    obsolete: tuple[str, ...] = ()

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

    def public(self) -> dict:
        return {
            "id": self.identity,
            "history": self.history,
            "questions": [question.public() for question in self.questions],
        }

    def score(self, answers: dict[str, str]) -> dict:
        unexpected = answers.keys() - {question.identity for question in self.questions}
        if unexpected:
            raise ValueError(f"Unknown questions in {self.identity}: {sorted(unexpected)}")
        results = tuple(
            question.score(answers.get(question.identity)) for question in self.questions
        )
        return {
            "round": self.identity,
            "questions": len(results),
            "correct": sum(result.correct for result in results),
            "stale": sum(result.stale for result in results),
            "missing": sum(result.missing for result in results),
            "answers": {
                question.identity: asdict(result)
                for question, result in zip(self.questions, results)
            },
        }


@dataclass(frozen=True)
class RecallScenario:
    identity: str
    rounds: tuple[RecallRound, ...]

    def public(self) -> dict:
        return {"scenario": self.identity, "rounds": [item.public() for item in self.rounds]}

    def score(self, condition: Condition, answers: dict[str, dict[str, str]]) -> dict:
        unexpected = answers.keys() - {item.identity for item in self.rounds}
        if unexpected:
            raise ValueError(f"Unknown rounds: {sorted(unexpected)}")
        scores = [item.score(answers.get(item.identity, {})) for item in self.rounds]
        return {
            "scenario": self.identity,
            "condition": condition.value,
            "synthetic": True,
            "rounds": scores,
            "questions": sum(item["questions"] for item in scores),
            "correct": sum(item["correct"] for item in scores),
            "stale": sum(item["stale"] for item in scores),
            "missing": sum(item["missing"] for item in scores),
        }


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


def decode_answers(text: str) -> dict[str, dict[str, str]]:
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"Duplicate answer key: {key}")
            result[key] = value
        return result

    answers = json.loads(text, object_pairs_hook=unique_object)
    if not isinstance(answers, dict):
        raise ValueError("Answers must be a round-to-answer object")
    for identity, values in answers.items():
        if not isinstance(values, dict) or any(
            not isinstance(value, str) for value in values.values()
        ):
            raise ValueError(f"Answers for {identity} must map question IDs to strings")
    return answers


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--answers", type=Path)
    parser.add_argument(
        "--condition", type=Condition, choices=tuple(Condition), default=Condition.BOUNDED
    )
    args = parser.parse_args()
    scenario = coding_scenario()
    result = scenario.public()
    if args.answers is not None:
        result = scenario.score(args.condition, decode_answers(args.answers.read_text()))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
