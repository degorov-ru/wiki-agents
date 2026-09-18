import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import semantic_eval


class SemanticEvalTests(unittest.TestCase):
    def test_fixed_corpus_has_tune_and_holdout(self):
        cases = json.loads((ROOT / "evaluation/semantic-cases.json").read_text())
        self.assertEqual(12, len(cases))
        self.assertEqual({"tune": 6, "holdout": 6}, {
            split: sum(case["split"] == split for case in cases) for split in ("tune", "holdout")
        })

    def test_evaluator_scores_and_saves_answers(self):
        cases = json.loads((ROOT / "evaluation/semantic-cases.json").read_text())
        answers = {case["id"]: case["expected"] for case in cases}
        with tempfile.TemporaryDirectory() as tmp, patch("model_client._attempt", return_value={"ok": True, "text": json.dumps(answers), "duration_s": 1}):
            output = Path(tmp) / "report.json"
            report = semantic_eval.evaluate("luna", output)
            self.assertEqual(12, report["runs"]["new"]["correct"])
            self.assertTrue(output.is_file())


if __name__ == "__main__":
    unittest.main()
