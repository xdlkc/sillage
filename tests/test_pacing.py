import tempfile
import threading
import time
import unittest
from pathlib import Path

from sillage.core import collect, read_json


class TimedClient:
    starts = []
    lock = threading.Lock()

    def __init__(self, provider, timeout):
        type(self).starts = []

    def complete(self, model, prompt, max_tokens, temperature):
        with self.lock:
            self.starts.append(time.monotonic())
        return {"ok": True, "text": "OK"}


class FailingClient(TimedClient):
    def complete(self, *args):
        super().complete(*args)
        return {"ok": False, "error": "http_502"}


class PacingTests(unittest.TestCase):
    def test_global_pacing_applies_across_workers(self):
        with tempfile.TemporaryDirectory() as root:
            collect(Path(root)/"run", {}, [{"id": "a/x"}],
                    [{"id": str(i), "text": "hi"} for i in range(6)],
                    workers=3, min_interval=0.025, client_factory=TimedClient, progress=lambda s: None)
        starts = sorted(TimedClient.starts)
        self.assertEqual(len(starts), 6)
        self.assertTrue(all(b-a >= 0.020 for a, b in zip(starts, starts[1:])))

    def test_failure_stop_preserves_full_plan(self):
        with tempfile.TemporaryDirectory() as root:
            run = Path(root)/"run"
            result = collect(run, {}, [{"id": "a/x"}],
                             [{"id": str(i), "text": "hi"} for i in range(20)],
                             workers=2, min_interval=0.01, fail_fast=2,
                             client_factory=FailingClient, progress=lambda s: None)
            self.assertTrue(result["stopped_after_errors"])
            self.assertLess(result["attempted_requests"], 20)
            self.assertEqual(result["planned_requests"], 20)
            public = read_json(run/"blind.json")
            self.assertEqual(len(public["endpoints"][0]["samples"]), 20)
            self.assertTrue(all(not s["ok"] for s in public["endpoints"][0]["samples"]))


if __name__ == "__main__":
    unittest.main()
