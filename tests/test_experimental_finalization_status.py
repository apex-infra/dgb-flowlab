import json
import subprocess
import unittest
from pathlib import Path


class ExperimentalFinalizationStatusTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parents[1]
        js = (root / "flowlab/web/app.js").read_text()
        start = js.index("function experimentalFinalizationStatus(")
        end = js.index("\nfunction renderExperimental(", start)
        cls.source = js[start:end]

    def status(self, play_name, jobs):
        payload = json.dumps({
            "play": play_name,
            "jobs": jobs,
        })

        script = (
            self.source
            + "\nconst input = " + payload + ";\n"
            + "console.log(JSON.stringify("
            + "experimentalFinalizationStatus("
            + "input.play, input.jobs)));\n"
        )

        result = subprocess.run(
            ["node", "-e", script],
            capture_output=True,
            text=True,
            check=True,
        )

        return json.loads(result.stdout)

    def test_settlement_pending(self):
        result = self.status("settlement_cycle", [])
        self.assertEqual(result["label"], "Reserve return")
        self.assertEqual(result["value"], "pending")

    def test_settlement_broadcast(self):
        jobs = [{
            "generated": {"phase": "reserve_return"},
            "state": "BROADCAST",
        }]

        result = self.status("settlement_cycle", jobs)

        self.assertEqual(result["label"], "Reserve return")
        self.assertEqual(result["value"], "0 / 1 confirmed")

    def test_settlement_confirmed(self):
        jobs = [{
            "generated": {"phase": "reserve_return"},
            "state": "CONFIRMED",
        }]

        result = self.status("settlement_cycle", jobs)

        self.assertEqual(result["label"], "Reserve return")
        self.assertEqual(result["value"], "1 / 1 confirmed")

    def test_other_play_preserves_finalization(self):
        jobs = [{
            "generated": {"phase": "finalization"},
            "state": "CONFIRMED",
        }]

        result = self.status("random_walk", jobs)

        self.assertEqual(result["label"], "Finalization")
        self.assertEqual(
            result["value"],
            "1 / 1 sweeps confirmed",
        )

    def test_other_play_ignores_reserve_return(self):
        jobs = [{
            "generated": {"phase": "reserve_return"},
            "state": "CONFIRMED",
        }]

        result = self.status("random_walk", jobs)

        self.assertEqual(result["label"], "Finalization")
        self.assertEqual(result["value"], "pending")


    def test_decision_phase_numbering(self):
        js = Path("flowlab/web/app.js").read_text()
        start = js.index("function experimentalDecisionStatus(")
        end = js.index(
            "\nfunction experimentalFinalizationStatus(",
            start,
        )

        script = js[start:end] + """
const snapshot = {
  play_name: "settlement_cycle",
  decision_targets: {
    workload: 2,
    return_workload: 2
  }
};

const job = {
  generated: {
    phase: "return_workload",
    decision_index: 1
  }
};

console.log(JSON.stringify(
  experimentalDecisionStatus(snapshot, job)
));
"""

        result = subprocess.run(
            ["node", "-e", script],
            capture_output=True,
            text=True,
            check=True,
        )

        status = json.loads(result.stdout)

        self.assertEqual(status["label"], "Return decision")
        self.assertEqual(status["value"], "2 / 2")



if __name__ == "__main__":
    unittest.main()
