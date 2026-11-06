import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from llm_blindtest.core import (BlindtestError, Client, ballot, collect, family, read_json,
                               redact, resolve, reveal, submit, write_json)


class FakeClient:
    def __init__(self, provider, timeout):
        pass

    def complete(self, model, prompt, max_tokens, temperature):
        if model["id"] == "broken/test":
            return {"ok": False, "error": "http_500"}
        return {"ok": True, "text": "我是 Claude / DeepSeek。最终答案是42。", "reported_model": model["id"],
                "latency_seconds": 0.123, "usage": {"prompt_tokens": 9}}


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.run = Path(self.temp.name) / "run"
        self.models = [{"id": "deepseek/test"}, {"id": "zenmux/anthropic/test"}, {"id": "broken/test"}]
        collect(self.run, {}, self.models, [{"id": "p", "text": "hi"}],
                progress=lambda x: None, client_factory=FakeClient)

    def tearDown(self):
        self.temp.cleanup()

    def guesses(self, correct=True):
        data = ballot(self.run)
        data.update(reviewer="test", method="fixture")
        private = read_json(self.run / "private/data.json")
        for g in data["guesses"]:
            if correct:
                g["family"] = private["assignments"][g["alias"]]["family"]
        return data

    def test_blind_export_has_no_response_metadata_or_self_identity(self):
        p = read_json(self.run / "blind.json")
        for e in p["endpoints"]:
            self.assertEqual(set(e), {"alias", "samples"})
            for s in e["samples"]:
                self.assertNotIn("reported_model", s)
                self.assertNotIn("latency_seconds", s)
                self.assertNotIn("usage", s)
                self.assertNotIn("Claude", s.get("answer", ""))
                self.assertNotIn("DeepSeek", s.get("answer", ""))

    def test_requires_submission(self):
        with self.assertRaises(BlindtestError):
            reveal(self.run)

    def test_score_excludes_failed_endpoint(self):
        submit(self.run, self.guesses())
        r = reveal(self.run)
        self.assertEqual(r["family_correct"], 2)
        self.assertEqual(r["eligible_endpoints"], 2)
        self.assertEqual(r["total_endpoints"], 3)
        self.assertEqual(r["family_accuracy"], 1)
        self.assertEqual(r["successful_requests"], 2)
        self.assertEqual(reveal(self.run), r)

    def test_unknown_counted_as_miss(self):
        submit(self.run, self.guesses(False))
        r = reveal(self.run)
        self.assertEqual(r["family_accuracy"], 0)
        self.assertEqual(r["guess_coverage"], 0)
        self.assertIsNone(r["accuracy_when_guessed"])

    def test_submission_cannot_be_replaced(self):
        g = self.guesses()
        submit(self.run, g)
        with self.assertRaises(BlindtestError):
            submit(self.run, g)

    def test_duplicate_guesses_rejected(self):
        g = self.guesses()
        g["guesses"][1] = g["guesses"][0]
        with self.assertRaises(BlindtestError):
            submit(self.run, g)

    def test_public_tampering_rejected(self):
        p = read_json(self.run / "blind.json")
        p["endpoints"].pop()
        write_json(self.run / "blind.json", p)
        with self.assertRaises(BlindtestError):
            ballot(self.run)

    def test_submission_tampering_rejected(self):
        g = self.guesses()
        submit(self.run, g)
        g["method"] = "changed"
        write_json(self.run / "submission.json", g)
        with self.assertRaises(BlindtestError):
            reveal(self.run)

    def test_mapping_tampering_rejected(self):
        submit(self.run, self.guesses())
        p = read_json(self.run / "private/data.json")
        p["salt"] = "changed"
        write_json(self.run / "private/data.json", p)
        with self.assertRaises(BlindtestError):
            reveal(self.run)

    def test_nan_confidence_rejected(self):
        g = self.guesses()
        g["guesses"][0]["confidence"] = float("nan")
        with self.assertRaises(BlindtestError):
            submit(self.run, g)


class ProtocolTests(unittest.TestCase):
    def test_gateway_family(self):
        self.assertEqual(family("zenmux/anthropic/claude"), "anthropic")
        self.assertEqual(family("deepseek/model"), "deepseek")

    def test_pi_credential_templates(self):
        with patch.dict("os.environ", {"TEST_BLIND_KEY": "test-value"}):
            self.assertEqual(resolve("${TEST_BLIND_KEY}"), "test-value")
            self.assertEqual(resolve("$TEST_BLIND_KEY"), "test-value")
            self.assertEqual(resolve("literal$$$!"), "literal$!")

    def test_redaction_does_not_replace_common_substrings(self):
        self.assertEqual(redact("metadata handling"), "metadata handling")
        self.assertNotIn("Sonnet", redact("I am Sonnet 5.5"))

    def test_nonstream_payload_and_final_text_only(self):
        response = io.BytesIO(json.dumps({"choices": [{"message": {"content": "answer", "reasoning_content": "private thought"},
                                                       "finish_reason": "stop"}]}).encode())
        p = {"api": "openai-completions", "apiKey": "secret", "baseUrl": "http://test/v1"}
        with patch("urllib.request.urlopen", return_value=response) as mock:
            r = Client(p).complete({"id": "x/y", "compat": {"maxTokensField": "max_completion_tokens"}}, "hi", 100, None)
            payload = json.loads(mock.call_args.args[0].data)
        self.assertTrue(r["ok"])
        self.assertEqual(payload["max_completion_tokens"], 100)
        self.assertNotIn("temperature", payload)
        self.assertNotIn("private thought", str(r))
        self.assertNotIn("secret", str(r))

    def test_http_error_does_not_leak_body(self):
        p = {"api": "openai-completions", "apiKey": "secret", "baseUrl": "http://test/v1"}
        error = urllib.error.HTTPError("http://test", 400, "secret detail", {}, io.BytesIO(b"secret body"))
        with patch("urllib.request.urlopen", side_effect=error):
            self.assertEqual(Client(p).complete({"id": "x/y"}, "hi", 100, None), {"ok": False, "error": "http_400"})

    def test_truncation_not_accepted_as_answer(self):
        response = io.BytesIO(json.dumps({"choices": [{"message": {"content": "partial"}, "finish_reason": "length"}]}).encode())
        p = {"api": "openai-completions", "apiKey": "", "baseUrl": "http://test/v1"}
        with patch("urllib.request.urlopen", return_value=response):
            r = Client(p).complete({"id": "x/y"}, "hi", 1, None)
        self.assertFalse(r["ok"])
        self.assertEqual(r["error"], "truncated")


if __name__ == "__main__":
    unittest.main()
