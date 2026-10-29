import unittest
from pathlib import Path


class OperationalGraphFrontendTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parents[1]
        cls.js = (root / "flowlab/web/app.js").read_text()

    def test_unified_renderer_exists(self):
        self.assertEqual(
            self.js.count("function drawOperationalStage("),
            1
        )

    def test_topology_adapter_used(self):
        self.assertIn(
            "buildOperationalTopology(flows, walletRoles)",
            self.js
        )

    def test_all_flows_rendered_together(self):
        self.assertIn(
            "drawOperationalStage(",
            self.js
        )
        self.assertNotIn(
            "drawStage(f, data.balances",
            self.js
        )

    def test_five_role_columns(self):
        start = self.js.index(
            "function drawOperationalStage("
        )
        end = self.js.index(
            "function drawStage(",
            start
        )
        renderer = self.js[start:end]

        for label in (
            "SOURCE",
            "STAGE",
            "HUBS",
            "WORKERS",
            "DESTINATIONS",
        ):
            self.assertIn(label, renderer)

    def test_unknown_roles_visible(self):
        self.assertIn(
            "graph.columns.UNASSIGNED.length",
            self.js
        )

    def test_broadcast_animation(self):
        self.assertIn(
            "activePaths.push(path)",
            self.js
        )
        self.assertIn(
            "beads.push({",
            self.js
        )

    def test_confirmed_history(self):
        self.assertIn(
            "confirmed ? 0.18",
            self.js
        )

    def test_planned_paths_faint(self):
        self.assertIn(
            'state === "PLANNED" ? "5 7"',
            self.js
        )

    def test_actual_job_endpoints_used(self):
        self.assertIn(
            "positions.get(edge.from)",
            self.js
        )
        self.assertIn(
            "positions.get(edge.to)",
            self.js
        )

    def test_existing_research_v1_preserved(self):
        self.assertIn(
            "function renderMixerResearchGraph(",
            self.js
        )
        self.assertIn(
            "function renderMixerResearchAnalytics(",
            self.js
        )


if __name__ == "__main__":
    unittest.main()
