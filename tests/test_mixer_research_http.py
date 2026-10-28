import copy
import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path

from flowlab.webdash import make_server


CONFIG = {
    "seeds": [42, 2049, 9999],
    "transaction_count": 100,
    "max_inputs": 6,
    "scenario_weights": {
        "single_owner": 40,
        "collaborative": 30,
        "mixed_ownership": 30,
    },
    "matched_input_profile": True,
}


class ResearchHTTPTests(unittest.TestCase):

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

        db = Path(self.temp.name) / "unused-research-test.db"

        self.server = make_server(
            db_path=str(db),
            port=0,
            control=True,
        )

        self.addCleanup(self.server.server_close)

        self.thread = threading.Thread(
            target=self.server.serve_forever,
            daemon=True,
        )
        self.thread.start()

        self.addCleanup(self.stop_server)

    def stop_server(self):
        self.server.shutdown()
        self.thread.join(timeout=5)

    def request(self, payload=None, token=None, origin=None):
        port = self.server.server_address[1]

        connection = http.client.HTTPConnection(
            "127.0.0.1", port, timeout=10
        )

        headers = {
            "Host": f"127.0.0.1:{port}",
            "Content-Type": "application/json",
            "X-Flowlab-Token": (
                self.server.token if token is None else token
            ),
        }

        if origin is not None:
            headers["Origin"] = origin

        connection.request(
            "POST",
            "/api/do/mixer-research-run",
            body=json.dumps(CONFIG if payload is None else payload),
            headers=headers,
        )

        response = connection.getresponse()
        status = response.status
        body = json.loads(response.read())
        connection.close()

        return status, body

    def test_research_action_registered(self):
        self.assertIn(
            "mixer-research-run",
            self.server.controller.actions,
        )

    def test_authenticated_research_run(self):
        status, result = self.request()

        self.assertEqual(status, 200)
        self.assertEqual(
            result["kind"], "mixer_dashboard_research_result"
        )
        self.assertEqual(
            result["execution"], "simulation_only"
        )
        self.assertEqual(
            result["aggregate"]["transaction_count"], 300
        )

    def test_token_required(self):
        status, result = self.request(token="wrong-token")

        self.assertEqual(status, 403)
        self.assertIn("token", result["error"])

    def test_foreign_origin_rejected(self):
        status, result = self.request(
            origin="http://untrusted.example"
        )

        self.assertEqual(status, 403)
        self.assertIn("origin", result["error"])

    def test_invalid_configuration_rejected(self):
        invalid = copy.deepcopy(CONFIG)
        invalid["wallet"] = "flab_source"

        status, result = self.request(payload=invalid)

        self.assertEqual(status, 400)
        self.assertIn("error", result)

    def test_response_excludes_private_records(self):
        status, result = self.request()

        self.assertEqual(status, 200)

        encoded = json.dumps(result)

        for forbidden in (
            "ground_truth",
            "utxo_owners",
            "transaction_participants",
            "participant_",
            "scenario_schedule",
            "restricted",
        ):
            self.assertNotIn(forbidden, encoded)

    def test_readonly_server_blocks_research(self):
        with tempfile.TemporaryDirectory() as directory:
            server = make_server(
                db_path=str(Path(directory) / "unused.db"),
                port=0,
                control=False,
            )

            thread = threading.Thread(
                target=server.serve_forever,
                daemon=True,
            )
            thread.start()

            try:
                port = server.server_address[1]
                connection = http.client.HTTPConnection(
                    "127.0.0.1", port, timeout=10
                )

                connection.request(
                    "POST",
                    "/api/do/mixer-research-run",
                    body=json.dumps(CONFIG),
                    headers={
                        "Host": f"127.0.0.1:{port}",
                        "Content-Type": "application/json",
                        "X-Flowlab-Token": server.token,
                    },
                )

                response = connection.getresponse()
                self.assertEqual(response.status, 403)
                connection.close()

            finally:
                server.shutdown()
                thread.join(timeout=5)
                server.server_close()



    def test_http_response_contains_public_graph(self):
        status, result = self.request()

        self.assertEqual(status, 200)

        graph = result["graph"]

        self.assertEqual(
            graph["kind"],
            "mixer_public_utxo_graph",
        )

        self.assertEqual(
            graph["summary"]["visible_transactions"],
            24,
        )

        self.assertTrue(graph["nodes"])
        self.assertTrue(graph["edges"])

        encoded = json.dumps(result)

        self.assertNotIn("ground_truth", encoded)
        self.assertNotIn("utxo_owners", encoded)
        self.assertNotIn("participant_", encoded)

if __name__ == "__main__":
    unittest.main()
