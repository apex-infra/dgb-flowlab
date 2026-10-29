import unittest
from pathlib import Path


class MixerLayoutFrontendTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parents[1]

        cls.html = (
            root / "flowlab/web/index.html"
        ).read_text()

        cls.css = (
            root / "flowlab/web/app.css"
        ).read_text()

        cls.js = (
            root / "flowlab/web/app.js"
        ).read_text()

    def test_mixer_page_preserved(self):
        self.assertIn(
            'id="tab-mixer"',
            self.html,
        )

    def test_full_width_scoped_to_mixer(self):
        self.assertIn(
            "#tab-mixer.page.mixer-page",
            self.css,
        )

    def test_details_element_present(self):
        self.assertIn(
            'id="mixer-research-raw-details"',
            self.html,
        )

    def test_details_closed_by_default(self):
        self.assertIn(
            '<details id="mixer-research-raw-details"',
            self.html,
        )
        self.assertNotIn(
            '<details id="mixer-research-raw-details" open',
            self.html,
        )

    def test_analytics_outside_details(self):
        start = self.html.index(
            '<details id="mixer-research-raw-details"'
        )

        end = self.html.index(
            '</details>',
            start,
        )

        analytics = self.html.index(
            'id="mixer-research-analytics"'
        )

        self.assertGreater(analytics, end)

    def test_graph_outside_details(self):
        start = self.html.index(
            '<details id="mixer-research-raw-details"'
        )

        end = self.html.index(
            '</details>',
            start,
        )

        graph = self.html.index(
            'id="mixer-research-graph-panel"'
        )

        self.assertGreater(graph, end)

    def test_existing_replay_preserved(self):
        self.assertIn(
            'id="mixer-replay-play"',
            self.html,
        )
        self.assertIn(
            'id="mixer-replay-pause"',
            self.html,
        )
        self.assertIn(
            'id="mixer-replay-speed"',
            self.html,
        )

    def test_existing_analytics_preserved(self):
        self.assertIn(
            'id="mixer-analytics-scenarios"',
            self.html,
        )

    def test_existing_graph_preserved(self):
        self.assertIn(
            'id="mixer-research-graph"',
            self.html,
        )

    def test_replay_renderer_preserved(self):
        self.assertIn(
            "const startReplay =",
            self.js,
        )

    def test_raw_metrics_remain_available(self):
        self.assertIn(
            'id="mixer-research-metrics"',
            self.html,
        )
        self.assertIn(
            'id="mixer-research-scenarios"',
            self.html,
        )


if __name__ == "__main__":
    unittest.main()
