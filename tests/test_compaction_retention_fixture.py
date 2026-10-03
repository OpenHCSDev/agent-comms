"""Behavioral checks of the offline recall oracle."""

from dataclasses import replace
import json
from pathlib import Path
import subprocess
import sys
import unittest
import hashlib

import pytest

from agent_comms.field_codec import FieldCodec
from agent_comms.native_entries import NativeEntry
from agent_comms.native_pi import NativeContextProof
from agent_comms.native_session_reopen import NativeSessionIdentity
from retained_native_fixture import RecordedNativeProbe
from test_backend_native_lifecycle import native_backend

from compaction_retention_fixture import (
    Condition,
    Question,
    RecordedAnswers,
    RecordedNativeProbes,
    Measurement,
    RecallScenario,
    coding_scenario,
    decode_answers,
)


class RecallMeasurementTests(unittest.TestCase):
    def setUp(self):
        self.scenario = coding_scenario()
        self.exact = {
            item.identity: {question.identity: question.expected for question in item.questions}
            for item in self.scenario.rounds
        }

    def test_conditions_share_the_same_oracle(self):
        for condition in Condition:
            with self.subTest(condition=condition):
                score = self.scenario.score(condition, RecordedAnswers(self.exact))
                self.assertEqual(
                    (score.questions, score.correct, score.stale, score.missing),
                    (21, 21, 0, 0),
                )

    def test_old_answers_fail_after_correction_and_goal_replacement(self):
        answers = {item.identity: self.exact["r1"] for item in self.scenario.rounds}
        score = self.scenario.score(Condition.RECENT_ONLY, RecordedAnswers(answers))
        self.assertEqual([item.correct for item in score.rounds], [7, 4, 2])
        self.assertEqual([item.stale for item in score.rounds], [0, 3, 5])

    def test_missing_answers_are_not_silently_removed_from_denominator(self):
        score = self.scenario.score(Condition.BOUNDED, RecordedAnswers({}))
        self.assertEqual((score.questions, score.correct, score.missing), (21, 0, 21))

    def test_nearly_matching_identifier_and_invented_completion_are_wrong(self):
        self.exact["r3"]["symbol"] = "frameowner"
        self.exact["r3"]["input"] = "COMPLETED"
        score = self.scenario.score(Condition.TASK_MEMORY, RecordedAnswers(self.exact))
        self.assertEqual(score.correct, 19)
        self.assertEqual(score.stale, 1)

    def test_new_question_uses_existing_scoring_contract(self):
        original = self.scenario.rounds[0]
        extra = Question(
            "artifact", "What is the artifact identity?", "artifact-3", "artifact ledger"
        )
        extended = replace(original, questions=original.questions + (extra,))
        answers = self.exact["r1"] | {"artifact": "artifact-3"}
        score = extended.score(answers)
        self.assertEqual((score.questions, score.correct), (8, 8))
        self.assertEqual(original.score(self.exact["r1"]).questions, 7)

    def test_round_and_scenario_totals_derive_from_their_outcomes(self):
        score = self.scenario.score(Condition.BOUNDED, RecordedAnswers(self.exact))
        first = score.rounds[0]
        source = replace(first.source, questions=first.source.questions[:1])
        shortened = source.score({"symbol": first.answer_values["symbol"]})
        changed_source = replace(score.source, rounds=(source,) + score.source.rounds[1:])
        changed = replace(score, source=changed_source, rounds=(shortened,) + score.rounds[1:])
        self.assertEqual((shortened.questions, shortened.correct), (1, 1))
        self.assertEqual((changed.questions, changed.correct), (15, 15))
        self.assertEqual((score.questions, score.correct), (21, 21))

    def test_caller_cannot_supply_an_independent_total(self):
        score = self.scenario.score(Condition.BOUNDED, RecordedAnswers(self.exact))
        for view in (score, score.rounds[0]):
            with self.subTest(view=view), self.assertRaises(TypeError):
                replace(view, correct=999)

    def test_unknown_round_or_question_is_rejected(self):
        for answers in ({"r4": {}}, {"r1": {"invented": "answer"}}):
            with self.subTest(answers=answers), self.assertRaises(ValueError):
                self.scenario.score(Condition.BOUNDED, RecordedAnswers(answers))

    def test_invalid_or_duplicate_recorded_answers_are_rejected(self):
        for text in (
            "[]",
            "null",
            "false",
            '{"r1":[]}',
            '{"r1":null}',
            '{"r1":{"symbol":false}}',
            '{"r1":{"symbol":17}}',
            '{"r1":{"symbol":null}}',
            '{"r1":{"symbol":[]}}',
            '{"r1":{},"r1":{}}',
            '{"r1":{"symbol":"a","symbol":"b"}}',
        ):
            with self.subTest(text=text), self.assertRaises(ValueError):
                decode_answers(text)
        decoded = decode_answers(json.dumps(self.exact))
        self.assertEqual(decoded, RecordedAnswers(self.exact))
        score = self.scenario.score(Condition.BOUNDED, decoded)
        self.assertEqual((score.questions, score.correct, score.missing), (21, 21, 0))
        missing = self.scenario.score(Condition.BOUNDED, decode_answers("{}"))
        self.assertEqual((missing.questions, missing.missing), (21, 21))

    def test_export_omits_answer_metadata_and_entrypoint_runs_without_provider(self):
        result = subprocess.run(
            [sys.executable, str(Path(__file__).with_name("compaction_retention_fixture.py"))],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
        public = json.loads(result.stdout)
        self.assertEqual(len(public["rounds"]), 3)
        for item in public["rounds"]:
            for question in item["questions"]:
                self.assertEqual(set(question), {"id", "prompt"})

    def test_held_out_probe_uses_public_questions_only(self):
        for item in self.scenario.rounds:
            questions = json.loads(item.probe_text().split("\n", 1)[1])
            self.assertEqual(questions, {
                "round": item.identity,
                "questions": [question.public() for question in item.questions],
            })

    def test_frozen_research_and_goal_traces_keep_distinctions_and_denominators(self):
        for name in ("research", "goal"):
            scenario = RecallScenario.read(Path(__file__).parent / "fixtures/retention" / f"{name}.json")
            answers = {item.identity: {q.identity: q.expected for q in item.questions}
                       for item in scenario.rounds}
            last = scenario.rounds[-1]
            alternative = next(q for q in last.questions if q.measurement is Measurement.ALTERNATIVE)
            action = next(q for q in last.questions if q.measurement is Measurement.ACTION)
            prohibition = next(q for q in last.questions if q.measurement is Measurement.PROHIBITION)
            answers[last.identity][alternative.identity] = "discarded-alternative"
            answers[last.identity][action.identity] = action.obsolete[0]
            del answers[last.identity][prohibition.identity]
            scored = scenario.score(Condition.TASK_MEMORY, RecordedAnswers(answers))
            measured = scored.measurement_totals()
            self.assertEqual((measured["recall"]["questions"], measured["recall"]["correct"]), (9, 9))
            self.assertEqual((measured["alternative"]["questions"], measured["alternative"]["correct"]), (3, 2))
            self.assertEqual((measured["action"]["questions"], measured["action"]["stale"]), (3, 1))
            self.assertEqual((measured["prohibition"]["questions"], measured["prohibition"]["missing"]), (3, 1))
            self.assertEqual((scored.questions, scored.correct, scored.missing), (18, 15, 1))
            for item in scenario.rounds:
                public = json.loads(item.probe_text().split("\n", 1)[1])
                self.assertTrue(all(set(q) == {"id", "prompt"} for q in public["questions"]))
            public = scored.public()
            self.assertEqual(public["measurements"], measured)
            answers[last.identity][prohibition.identity] = "yes"
            answers[last.identity][action.identity] = "inspect-held-out" if name == "research" else "retry-input-9"
            invalid = scenario.score(Condition.TASK_MEMORY, RecordedAnswers(answers)).measurement_totals()
            self.assertEqual((invalid["action"]["correct"], invalid["action"]["stale"]), (2, 0))
            self.assertEqual((invalid["prohibition"]["correct"], invalid["prohibition"]["missing"]), (2, 0))

    def test_scenario_loader_rejects_ambiguous_or_missing_source_oracles(self):
        scenario = coding_scenario()
        first = scenario.rounds[0]
        with self.assertRaises(ValueError):
            replace(first, questions=first.questions + first.questions[:1])
        with self.assertRaises(ValueError):
            replace(scenario, rounds=scenario.rounds + scenario.rounds[:1])
        with self.assertRaises(ValueError):
            replace(first.questions[0], evidence_ref="")
        with self.assertRaises(ValueError):
            replace(first.questions[0], obsolete=(first.questions[0].expected,))

    def test_cli_uses_frozen_scenario_for_public_probe_export(self):
        path = Path(__file__).parent / "fixtures/retention/research.json"
        result = subprocess.run(
            [sys.executable, str(Path(__file__).with_name("compaction_retention_fixture.py")),
             "--scenario-file", str(path), "--probe-prompts"],
            check=True, capture_output=True, text=True, timeout=5,
        )
        expected = RecallScenario.read(path)
        self.assertEqual(json.loads(result.stdout), {item.identity: item.probe_text() for item in expected.rounds})


async def test_recorded_native_recall_consumes_original_probe_and_preserves_source(native_backend):
    """Controlled answers check plumbing, never model retention quality."""
    owner = native_backend
    scenario = coding_scenario()
    first = scenario.rounds[0]
    seeded = await owner.run("\n".join(first.history))
    assert seeded[-1].ok
    owner.provider.text = json.dumps({
        first.identity: {question.identity: question.expected for question in first.questions}
    })
    completed = await owner.run(first.probe_text())
    assert completed[-1].ok and owner.provider.posts == 2
    header, entries = NativeEntry.read_evidence(owner.session)
    with NativeEntry.open_evidence(owner.session) as evidence:
        context = NativeContextProof.read_evidence(owner.session, owner.starts[-1][1], evidence=evidence)
        answer, _ = RecordedNativeProbe.answer_for_input(evidence, context)
    reference = RecordedNativeProbes({first.identity: RecordedNativeProbe(
        NativeSessionIdentity(header.id, str(owner.session)), owner.starts[-1][1], answer.id
    )})
    original = {
        path: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in (owner.session, Path(str(owner.session) + ".input-proof"))
    }
    decoded = FieldCodec.decode(RecordedNativeProbes, FieldCodec.encode(reference))
    result = scenario.score_native(Condition.FULL_CONTEXT, decoded)
    assert (result["questions"], result["correct"], result["missing"]) == (21, 7, 14)
    observed = result["native_probes"][first.identity]
    assert observed["context"]["inputId"] == owner.starts[-1][1]
    assert observed["answer"]["id"] == answer.id
    assert observed["answer_text"] == owner.provider.text
    assert result["provider_prompt_presence"][first.identity]["evaluated"] is False
    with pytest.raises(ValueError, match="frozen held-out"):
        scenario.rounds[1].score_native(decoded.rounds[first.identity])
    bad_answer = replace(decoded.rounds[first.identity], answer_entry_id=answer.parent_id)
    with pytest.raises(ValueError, match="successful native terminal"):
        bad_answer.observe()
    assert {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in original} == original
    assert owner.provider.posts == len(owner.starts) == 2

    report = {
        "scope": "controlled native recorded-result consumer; not S4 model-retention quality",
        "ok": True,
        "provider_posts": owner.provider.posts,
        "recorded_rounds": 1,
        "questions": result["questions"],
        "controlled_correct": result["correct"],
        "missing": result["missing"],
        "original_source_sha256": original[owner.session],
        "input_proof_sha256": original[Path(str(owner.session) + ".input-proof")],
        "original_source_unchanged": True,
        "question_mismatch_refused": True,
        "nonterminal_answer_refused": True,
    }
    (owner.root.parent / "recorded-recall-receipt.json").write_text(json.dumps(report, indent=2))


if __name__ == "__main__":
    unittest.main()
