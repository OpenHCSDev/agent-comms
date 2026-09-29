"""Provider-free checks of the recall measurement, not of model retention."""

from dataclasses import replace
import json
from pathlib import Path
import subprocess
import sys
import unittest

from compaction_retention_fixture import Condition, Question, coding_scenario, decode_answers


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
                score = self.scenario.score(condition, self.exact)
                self.assertEqual(
                    (score["questions"], score["correct"], score["stale"], score["missing"]),
                    (21, 21, 0, 0),
                )

    def test_old_answers_fail_after_correction_and_goal_replacement(self):
        answers = {item.identity: self.exact["r1"] for item in self.scenario.rounds}
        score = self.scenario.score(Condition.RECENT_ONLY, answers)
        self.assertEqual([item["correct"] for item in score["rounds"]], [7, 4, 2])
        self.assertEqual([item["stale"] for item in score["rounds"]], [0, 3, 5])

    def test_missing_answers_are_not_silently_removed_from_denominator(self):
        score = self.scenario.score(Condition.BOUNDED, {})
        self.assertEqual((score["questions"], score["correct"], score["missing"]), (21, 0, 21))

    def test_nearly_matching_identifier_and_invented_completion_are_wrong(self):
        self.exact["r3"]["symbol"] = "frameowner"
        self.exact["r3"]["input"] = "COMPLETED"
        score = self.scenario.score(Condition.TASK_MEMORY, self.exact)
        self.assertEqual(score["correct"], 19)
        self.assertEqual(score["stale"], 1)

    def test_new_question_uses_existing_scoring_contract(self):
        original = self.scenario.rounds[0]
        extra = Question(
            "artifact", "What is the artifact identity?", "artifact-3", "artifact ledger"
        )
        extended = replace(original, questions=original.questions + (extra,))
        answers = self.exact["r1"] | {"artifact": "artifact-3"}
        score = extended.score(answers)
        self.assertEqual((score["questions"], score["correct"]), (8, 8))
        self.assertEqual(original.score(self.exact["r1"])["questions"], 7)

    def test_unknown_round_or_question_is_rejected(self):
        for answers in ({"r4": {}}, {"r1": {"invented": "answer"}}):
            with self.subTest(answers=answers), self.assertRaises(ValueError):
                self.scenario.score(Condition.BOUNDED, answers)

    def test_invalid_or_duplicate_recorded_answers_are_rejected(self):
        for text in (
            "[]",
            '{"r1":[]}',
            '{"r1":{"symbol":false}}',
            '{"r1":{},"r1":{}}',
            '{"r1":{"symbol":"a","symbol":"b"}}',
        ):
            with self.subTest(text=text), self.assertRaises(ValueError):
                decode_answers(text)
        self.assertEqual(decode_answers(json.dumps(self.exact)), self.exact)

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


if __name__ == "__main__":
    unittest.main()
