import copy
import tempfile
import unittest
from pathlib import Path

from vestige.core import BlindtestError, collect, digest, read_json, reveal, submit, write_json
from vestige.fingerprint import classify, fit, load_reference, predict_command, train_command


class SampleClient:
    def __init__(self, provider, timeout):
        pass

    def complete(self, model, prompt, max_tokens, temperature):
        text = "红苹果的故事。请确认。" if model["id"] == "a/one" else "Blue oceans! Absolutely."
        return {"ok": True, "text": text + prompt}


class FingerprintTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.models = [{"id": "a/one"}, {"id": "b/two"}]
        self.prompts = [{"id": f"p{i}", "text": f"question {i}"} for i in range(3)]
        self.ref = self.root / "reference"
        self.model_path = self.root / "model.json"
        collect(self.ref, {}, self.models, self.prompts, repeats=2, reference=True,
                workers=2, progress=lambda s: None, client_factory=SampleClient)
        train_command(self.ref, self.model_path)
        self.model = read_json(self.model_path)

    def tearDown(self):
        self.temp.cleanup()

    def blind(self):
        run = self.root / "blind"
        collect(run, {}, self.models, self.prompts, trials=3, workers=2,
                progress=lambda s: None, client_factory=SampleClient)
        return run

    def test_full_reference_predict_submit_reveal_workflow(self):
        run = self.blind()
        predictions = self.root / "predictions.json"
        predict_command(run, self.model_path, predictions, 3, digest(self.model))
        submit(run, read_json(predictions))
        report = reveal(run)
        self.assertEqual(report["total_endpoints"], 6)
        self.assertEqual(report["family_correct"], 6)
        self.assertEqual(report["overall_accuracy"], 1)

    def test_predict_does_not_need_private_files(self):
        run = self.blind()
        (run / "private/data.json").unlink()
        predict_command(run, self.model_path, self.root / "guesses.json", 3)
        self.assertEqual(len(read_json(self.root / "guesses.json")["guesses"]), 6)

    def test_metadata_cannot_affect_prediction(self):
        run = self.blind()
        public = read_json(run / "blind.json")
        before = classify(public, self.model, 3)
        for e in public["endpoints"]:
            e["model"] = "bogus"
            for s in e["samples"]:
                s.update(model="bogus", latency_seconds=999, usage={"prompt_tokens": 999})
        self.assertEqual(classify(public, self.model, 3), before)

    def test_missing_probes_abstain(self):
        run = self.blind()
        public = read_json(run / "blind.json")
        public["endpoints"][0]["samples"] = public["endpoints"][0]["samples"][:1]
        result = classify(public, self.model, 3)
        self.assertEqual(result["guesses"][0]["family"], "unknown")

    def test_no_global_one_to_one_matching(self):
        run = self.blind()
        public = read_json(run / "blind.json")
        first = copy.deepcopy(public["endpoints"][0])
        second = copy.deepcopy(first)
        second["alias"] = "different-id"
        public["endpoints"] = [first, second]
        guesses = classify(public, self.model, 3)["guesses"]
        self.assertEqual(guesses[0]["model"], guesses[1]["model"])

    def test_reference_cannot_be_blind_run(self):
        run = self.blind()
        with self.assertRaises(BlindtestError):
            load_reference(run)

    def test_frozen_classifier_digest_enforced(self):
        run = self.blind()
        with self.assertRaises(BlindtestError):
            predict_command(run, self.model_path, self.root / "g.json", 3, "wrong-hash")

    def test_prompt_text_mismatch_rejected(self):
        run = self.blind()
        public = read_json(run / "blind.json")
        public["endpoints"][0]["samples"][0]["prompt"] = "changed"
        with self.assertRaises(BlindtestError):
            classify(public, self.model, 3)

    def test_reference_file_tamper_rejected(self):
        r = read_json(self.ref / "reference.json")
        r["examples"][0]["answer"] = "tampered"
        write_json(self.ref / "reference.json", r)
        with self.assertRaises(BlindtestError):
            load_reference(self.ref)

    def test_missing_reference_model_probe_is_dropped(self):
        data = load_reference(self.ref)
        data["examples"] = [e for e in data["examples"] if not (e["model"] == "a/one" and e["prompt_id"] == "p0")]
        with self.assertRaises(BlindtestError):
            fit(data)  # Only two complete probes remain.


if __name__ == "__main__":
    unittest.main()
