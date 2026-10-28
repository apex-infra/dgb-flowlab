import unittest
from pathlib import Path


class MixerGraphFrontendTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parents[1]
        cls.html = (
            root / "flowlab/web/index.html"
        ).read_text()
        cls.js = (
            root / "flowlab/web/app.js"
        ).read_text()
        cls.css = (
            root / "flowlab/web/app.css"
        ).read_text()

    def test_graph_panel_present(self):
        self.assertIn(
            'id="mixer-research-graph-panel"', self.html
        )

    def test_svg_present(self):
        self.assertIn(
            'id="mixer-research-graph"', self.html
        )

    def test_telemetry_present(self):
        self.assertIn(
            'id="mixer-research-graph-telemetry"', self.html
        )

    def test_selection_present(self):
        self.assertIn(
            'id="mixer-research-graph-selection"', self.html
        )

    def test_renderer_defined_once(self):
        self.assertEqual(
            self.js.count(
                "function renderMixerResearchGraph("
            ),
            1,
        )

    def test_renderer_connected_to_request(self):
        self.assertIn(
            "renderMixerResearchGraph(response.graph);",
            self.js,
        )

    def test_svg_uses_safe_text_content(self):
        self.assertIn(
            "label.textContent = isTx ?", self.js
        )
        self.assertIn(
            "title.textContent = isTx ?", self.js
        )

    def test_clickable_transaction_nodes(self):
        self.assertIn(
            "inspectTransaction(node)", self.js
        )

    def test_stable_positions(self):
        self.assertIn(
            "const txPositions = new Map();", self.js
        )
        self.assertIn(
            "const positions = new Map(txPositions);",
            self.js,
        )

    def test_directional_edges(self):
        self.assertIn(
            '"marker-end": "url(#mixer-research-arrow)"',
            self.js,
        )

    def test_animation_and_accessibility(self):
        self.assertIn(
            "@keyframes mixer-graph-flow", self.css
        )
        self.assertIn(
            "prefers-reduced-motion: reduce",
            self.css,
        )
        self.assertIn(
            'group.setAttribute("tabindex", "0")',
            self.js,
        )

    def test_existing_research_form_preserved(self):
        self.assertIn(
            'id="mixer-research-form"', self.html
        )
        self.assertIn(
            'id="mixer-research-results"', self.html
        )


if __name__ == "__main__":
    unittest.main()
