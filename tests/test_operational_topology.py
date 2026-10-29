import json
import subprocess
import unittest
from pathlib import Path


class OperationalTopologyTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parents[1]
        js = (root / "flowlab/web/app.js").read_text()
        start = js.index("function buildOperationalTopology(")
        end = js.index("\nfunction drawStage(", start)
        cls.function_source = js[start:end]


    def build(self, flows, roles):
        payload = json.dumps({
            "flows": flows,
            "roles": roles,
        })

        script = (
            self.function_source
            + chr(10)
            + "const input = "
            + payload
            + ";"
            + chr(10)
            + "console.log(JSON.stringify("
            + "buildOperationalTopology(input.flows, input.roles)));"
        )

        result = subprocess.run(
            ["node", "-e", script],
            capture_output=True,
            text=True,
            check=True,
        )

        return json.loads(result.stdout)

    def test_five_configured_role_columns(self):
        roles = {
            "reserve": ["source"],
            "stage": ["stage"],
            "hubs": ["hub"],
            "workers": ["worker"],
            "destinations": ["destination"],
        }

        flows = [{
            "source": "source",
            "wallets": ["stage", "hub", "worker"],
            "dest": "destination",
            "jobs": [],
        }]

        graph = self.build(flows, roles)

        for column in (
            "SOURCE", "STAGE", "HUBS",
            "WORKERS", "DESTINATIONS"
        ):
            self.assertEqual(len(graph["columns"][column]), 1)

    def test_multiple_flows_preserved(self):
        flows = [
            {"jobs": [
                {"from": "a", "to": "b",
                 "seq": 0, "state": "BROADCAST"}
            ]},
            {"jobs": [
                {"from": "b", "to": "c",
                 "seq": 0, "state": "CONFIRMED"}
            ]},
        ]

        graph = self.build(flows, {})

        self.assertEqual(len(graph["edges"]), 2)
        self.assertEqual(len(graph["nodes"]), 3)
        self.assertEqual(graph["edges"][0]["id"], "0:0")
        self.assertEqual(graph["edges"][1]["id"], "1:0")

    def test_unknown_wallets_not_misclassified(self):
        graph = self.build(
            [{"jobs": [
                {"from": "unknown_a", "to": "unknown_b",
                 "seq": 0, "state": "PLANNED"}
            ]}],
            {},
        )

        self.assertEqual(
            len(graph["columns"]["UNASSIGNED"]), 2
        )
        self.assertFalse(graph["columns"]["DESTINATIONS"])

    def test_transaction_state_preserved(self):
        graph = self.build(
            [{"jobs": [{
                "from": "a", "to": "b",
                "seq": 7,
                "state": "CONFIRMED",
                "txid": "synthetic-tx",
                "confs": 6,
                "fee": 400,
            }]}],
            {},
        )

        edge = graph["edges"][0]

        self.assertEqual(edge["state"], "CONFIRMED")
        self.assertEqual(edge["txid"], "synthetic-tx")
        self.assertEqual(edge["confirmations"], 6)
        self.assertEqual(edge["fee"], 400)

    def test_no_invented_edges(self):
        graph = self.build(
            [{"source": "a", "dest": "b", "jobs": []}],
            {"reserve": ["a"], "destinations": ["b"]},
        )

        self.assertEqual(graph["edges"], [])
        self.assertEqual(len(graph["nodes"]), 2)

    def test_invalid_job_endpoints_skipped(self):
        graph = self.build(
            [{"jobs": [
                {"from": "a", "to": None, "seq": 0},
                {"from": "a", "to": "b", "seq": 1},
            ]}],
            {},
        )

        self.assertEqual(len(graph["edges"]), 1)



    def test_settlement_is_operation_not_wallet_edge(self):
        graph = self.build([{"jobs": [{
            "from": "flab_stage",
            "to": None,
            "to_label": "Settlement (3 destinations)",
            "seq": 4,
            "state": "CONFIRMED",
            "txid": "synthetic-settlement",
            "confs": 6,
        }]}], {"stage": ["flab_stage"]})

        self.assertEqual(graph["edges"], [])
        self.assertEqual(len(graph["operations"]), 1)
        op = graph["operations"][0]
        self.assertEqual(op["from"], "flab_stage")
        self.assertEqual(op["state"], "CONFIRMED")
        self.assertEqual(op["confirmations"], 6)
        self.assertEqual(len(graph["nodes"]), 1)

if __name__ == "__main__":
    unittest.main()
