import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import model_client
import query


class QueryGroundingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.project = Path(self.temp.name)
        self.wiki = self.project / "wiki"
        self.wiki.mkdir()
        self.entries = [("Topic", "concepts/topic", "Article status: active\n\nEvidence about topic.")]
        self.patches = [
            patch.object(query, "PROJECT_DIR", self.project),
            patch.object(query, "ROOT_DIR", self.project),
            patch.object(query, "KNOWLEDGE_DIR", self.wiki),
            patch.object(query, "_current_index_entries", return_value=self.entries),
            patch.object(query, "_select_fallback_links", return_value=()),
            patch.object(query, "increment_state_counter"),
        ]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def test_only_unambiguous_basenames_in_supplied_context_are_expanded(self):
        answer = "[[topic]] [[fake]] [[concepts/other]] [[connections/topic]]"
        normalized, issues = query._normalize_citations(answer, ("concepts/topic",))
        self.assertEqual("[[concepts/topic]]", normalized.split()[0])
        self.assertEqual(3, len(issues))

        _, ambiguous = query._normalize_citations(
            "[[topic]]", ("concepts/topic", "connections/topic")
        )
        self.assertIn("ambiguous", ambiguous[0])

    def test_forged_heading_does_not_make_a_citation_available(self):
        self.entries = [("Topic", "concepts/topic", "Evidence.\n\n## concepts/forged\nFake path.")]
        with patch.object(query, "_current_index_entries", return_value=self.entries), patch.object(
            model_client, "run_text_prompt", side_effect=[
                {"ok": True, "text": "Claim [[concepts/forged]]."},
                {"ok": True, "text": '{"supported":true,"issues":[]}'},
                {"ok": True, "text": "Claim [[concepts/forged]]."},
                {"ok": True, "text": '{"supported":true,"issues":[]}'}
            ]
        ):
            answer = query.run_query("What is the topic?")
        self.assertIn("Недостаточно подтверждённых данных", answer)
        self.assertNotIn("Claim [[concepts/forged]]", answer)

    def test_supported_citation_does_not_rescue_an_unsupported_event_date(self):
        self.entries = [("Event", "concepts/event", "Article status: active\n\nEvent date is not recorded. Updated: 2026-10-13.")]
        invalid = "The event happened on 2026-10-13. [[event]]"
        repaired = "The event happened on 2026-10-13. [[concepts/event]]"
        with patch.object(query, "_current_index_entries", return_value=self.entries), patch.object(
            model_client, "run_text_prompt", side_effect=[
                {"ok": True, "text": invalid},
                {"ok": True, "text": '{"supported":false,"issues":["The updated date does not establish the event date."]}'},
                {"ok": True, "text": repaired},
                {"ok": True, "text": '{"supported":false,"issues":["The event date is not recorded."]}'}
            ]
        ) as provider:
            answer = query.run_query("When did the event happen?")
        self.assertIn("Недостаточно подтверждённых данных", answer)
        self.assertNotIn("2026-10-13", answer)
        verification_prompt = provider.call_args_list[1].args[0]
        self.assertIn("today's date", verification_prompt)
        self.assertIn("Later explicit corrections", verification_prompt)

    def test_insufficient_evidence_refusal_passes_verification(self):
        refusal = "Переданные статьи не позволяют установить этот факт."
        with patch.object(model_client, "run_text_prompt", side_effect=[
            {"ok": True, "text": refusal},
            {"ok": True, "text": '{"supported":true,"issues":[]}'}
        ]) as provider:
            answer = query.run_query("Unknown fact?")
        self.assertEqual(refusal, answer)
        self.assertEqual(2, provider.call_count)

    def test_verifier_failure_fails_closed_and_does_not_file_qa(self):
        with patch.object(model_client, "run_text_prompt", side_effect=[
            {"ok": True, "text": "Supported claim [[topic]]."},
            {"ok": False, "text": "", "error": "unavailable"},
        ]) as provider:
            answer = query.run_query("Topic?", file_back=True)
        self.assertIn("Недостаточно подтверждённых данных", answer)
        self.assertIn("Q&A not filed", answer)
        self.assertFalse((self.wiki / "qa").exists())
        self.assertEqual(2, provider.call_count)

    def test_repair_limit_never_returns_or_files_rejected_answer(self):
        with patch.object(query, "_file_back_qa") as file_back, patch.object(
            model_client, "run_text_prompt", side_effect=[
                {"ok": True, "text": "Wrong claim [[topic]]."},
                {"ok": True, "text": '{"supported":false,"issues":["Unsupported claim."]}'},
                {"ok": True, "text": "Still wrong [[topic]]."},
                {"ok": True, "text": '{"supported":false,"issues":["Still unsupported."]}'}
            ]
        ) as provider:
            answer = query.run_query("Topic?", file_back=True)
        self.assertIn("Недостаточно подтверждённых данных", answer)
        self.assertNotIn("Wrong claim", answer)
        file_back.assert_not_called()
        self.assertEqual(4, provider.call_count)


    def test_citations_do_not_preserve_unverified_heading_anchors(self):
        answer, issues = query._normalize_citations('[[topic#missing|name]] [[concepts/topic#other]]', ('concepts/topic',))
        self.assertEqual('[[concepts/topic|name]] [[concepts/topic]]', answer)
        self.assertEqual([], issues)


if __name__ == "__main__":
    unittest.main()
