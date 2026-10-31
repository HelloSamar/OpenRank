"""The quiz question validator, tested against the shared cases in tests/fixtures.

The same file is meant to be used by any other runtime that posts quizzes (for example a
Cloudflare Worker), so that all of them accept and refuse exactly the same questions.
"""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import quiz_bot as qb

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "quiz_validation_cases.json"
DATA = json.loads(FIXTURE.read_text(encoding="utf-8"))


class SharedCaseTests(unittest.TestCase):
    def test_every_shared_case(self):
        self.assertGreaterEqual(len(DATA["cases"]), 30)
        for case in DATA["cases"]:
            problem = qb.validate_question(case["item"])
            if case["valid"]:
                self.assertEqual(problem, "", f"{case['name']}: should be valid but: {problem}")
            else:
                self.assertNotEqual(problem, "", f"{case['name']}: should be refused")
                self.assertIn(case["problem_contains"], problem, case["name"])

    def test_the_fixture_describes_the_limits_the_code_uses(self):
        limits = DATA["limits"]
        self.assertEqual(limits["question"], qb.MAX_QUESTION_LENGTH)
        self.assertEqual(limits["question_prefix_reserve"], qb.QUESTION_PREFIX_RESERVE)
        self.assertEqual(limits["option"], qb.MAX_OPTION_LENGTH)
        self.assertEqual(limits["explanation"], qb.MAX_EXPLANATION_LENGTH)
        self.assertEqual((limits["options_min"], limits["options_max"]),
                         (qb.MIN_OPTIONS, qb.MAX_OPTIONS))
        self.assertTrue(qb.SHOW_QUESTION_NUMBERS)

    def test_the_limits_are_the_ones_telegram_documents(self):
        """Telegram: question 1-300, 2-10 options of 1-100 characters, explanation 0-200."""
        self.assertEqual(qb.MAX_QUESTION_LENGTH, 300)
        self.assertEqual((qb.MIN_OPTIONS, qb.MAX_OPTIONS), (2, 10))
        self.assertEqual(qb.MAX_OPTION_LENGTH, 100)
        self.assertEqual(qb.MAX_EXPLANATION_LENGTH, 200)


class PythonOnlyTests(unittest.TestCase):
    def test_a_float_index_written_as_1_point_0_is_refused(self):
        """Python tells 1.0 from 1 when reading JSON; JavaScript cannot, so this is not shared."""
        self.assertIn("integer", qb.validate_question(["Q?", ["a", "b"], 1.0]))


class FilterTests(unittest.TestCase):
    def test_only_valid_questions_come_out_and_skips_are_counted(self):
        items = [["Q one?", ["a", "b"], 0], ["", ["a", "b"], 0], ["Q two?", ["a", "b"], 5],
                 ["Q three?", ["x", "y", "z"], 2, "because"]]
        valid, skipped = qb.filter_valid(items)
        self.assertEqual(skipped, 2)
        self.assertEqual([q[0] for q in valid], ["Q one?", "Q three?"])
        self.assertEqual(valid[0][3], "")                      # missing explanation becomes ""
        self.assertEqual(valid[1][3], "because")

    def test_the_same_question_and_answer_twice_is_posted_once(self):
        items = [["Capital of France?", ["Paris", "Rome"], 0],
                 ["Capital of France?", ["Rome", "Paris"], 1],        # same question, same answer
                 ["Capital of France?", ["Paris", "Rome"], 1]]        # same question, other answer
        valid, skipped = qb.filter_valid(items)
        self.assertEqual((len(valid), skipped), (2, 1))

    def test_a_question_can_never_reach_telegram_over_the_limits(self):
        weird = [["Q?", ["a", "b"], 0, "x" * 500], ["y" * 400, ["a", "b"], 0],
                 ["Q?", ["z" * 150, "b"], 0], ["Q?", [str(i) for i in range(30)], 0]]
        self.assertEqual(qb.filter_valid(weird), ([], 4))

    def test_nothing_that_is_not_a_list_crashes_the_filter(self):
        junk = [None, 5, "text", {"a": 1}, [], [1], ["Q?", None, 0], ["Q?", ["a", "b"], None]]
        self.assertEqual(qb.filter_valid(junk), ([], len(junk)))


class RealQuizFileTests(unittest.TestCase):
    def test_every_stored_quiz_is_fully_valid(self):
        folder = Path(__file__).resolve().parent.parent / "quizzes"
        files = sorted(folder.glob("*.json"))
        self.assertTrue(files)
        for path in files:
            items = json.loads(path.read_text(encoding="utf-8"))
            valid, skipped = qb.filter_valid(items)
            self.assertEqual(skipped, 0, f"{path.name}: {skipped} refused question(s)")
            self.assertTrue(valid, path.name)


if __name__ == "__main__":
    unittest.main()
