import unittest
from pathlib import Path


class MixerAnalyticsFrontendTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parents[1]
        cls.html = (root / "flowlab/web/index.html").read_text()
        cls.js = (root / "flowlab/web/app.js").read_text()
        cls.css = (root / "flowlab/web/app.css").read_text()

    def test_analytics_panel_exists(self):
        self.assertIn(
            'id="mixer-research-analytics"', self.html
        )

    def test_classification_panel_exists(self):
        self.assertIn(
            'id="mixer-analytics-classification"', self.html
        )

    def test_precision_panel_exists(self):
        self.assertIn(
            'id="mixer-analytics-precision"', self.html
        )

    def test_scenario_panel_exists(self):
        self.assertIn(
            'id="mixer-analytics-scenarios"', self.html
        )

    def test_summary_panel_exists(self):
        self.assertIn(
            'id="mixer-analytics-summary"', self.html
        )

    def test_renderer_defined_once(self):
        self.assertEqual(
            self.js.count("function renderMixerResearchAnalytics("),
            1,
        )

    def test_renderer_connected(self):
        self.assertIn(
            "renderMixerResearchAnalytics(response);", self.js
        )

    def test_precision_fields(self):
        for field in (
            "p.micro",
            "p.mean",
            "p.population_stddev",
            "p.minimum",
            "p.maximum",
            "p.defined_seeds",
            "p.undefined_seeds",
        ):
            self.assertIn(field, self.js)

    def test_classification_fields(self):
        self.assertIn("counts.tp", self.js)
        self.assertIn("counts.fp", self.js)

    def test_scenario_precision(self):
        self.assertIn("item.metrics?.precision", self.js)

    def test_safe_rendering(self):
        self.assertIn("title.textContent = label;", self.js)
        self.assertIn("label.textContent = name.replaceAll", self.js)

    def test_reduced_motion(self):
        self.assertIn(
            ".mixer-analytics-fill {", self.css
        )
        self.assertIn(
            "prefers-reduced-motion: reduce", self.css
        )

    def test_existing_graph_preserved(self):
        self.assertIn(
            'id="mixer-research-graph"', self.html
        )
        self.assertIn(
            "renderMixerResearchGraph(response.graph);", self.js
        )


if __name__ == "__main__":
    unittest.main()
