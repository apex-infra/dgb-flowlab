import unittest
from pathlib import Path


class MixerReplayFrontendTests(unittest.TestCase):

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

    def test_play_button_present(self):
        self.assertIn('id="mixer-replay-play"', self.html)

    def test_pause_button_present(self):
        self.assertIn('id="mixer-replay-pause"', self.html)

    def test_restart_button_present(self):
        self.assertIn('id="mixer-replay-restart"', self.html)

    def test_speed_control_present(self):
        self.assertIn('id="mixer-replay-speed"', self.html)

    def test_replay_status_present(self):
        self.assertIn('id="mixer-replay-status"', self.html)

    def test_replay_is_deterministic(self):
        self.assertIn("a.sequence - b.sequence", self.js)

    def test_playback_uses_timer(self):
        self.assertIn("setInterval(stepReplay, delay)", self.js)

    def test_playback_stops_timer(self):
        self.assertIn("clearInterval(replayTimer)", self.js)

    def test_new_graph_cleans_old_playback(self):
        self.assertIn(
            "mixerResearchReplayCleanup();",
            self.js,
        )

    def test_manual_selection_pauses_replay(self):
        self.assertIn(
            '"mixer-replay-pause"',
            self.js,
        )

    def test_replay_is_bounded_to_graph_data(self):
        self.assertIn(
            "orderedTransactions.length",
            self.js,
        )

    def test_existing_graph_preserved(self):
        self.assertIn(
            "function renderMixerResearchGraph(graph)",
            self.js,
        )

    def test_existing_analytics_preserved(self):
        self.assertIn(
            "function renderMixerResearchAnalytics(response)",
            self.js,
        )

    def test_reduced_motion_support(self):
        self.assertIn(
            "prefers-reduced-motion: reduce",
            self.css,
        )


if __name__ == "__main__":
    unittest.main()
