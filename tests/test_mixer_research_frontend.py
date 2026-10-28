import unittest
from pathlib import Path


class MixerResearchFrontendTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parents[1]
        cls.html = (root / "flowlab/web/index.html").read_text()
        cls.js = (root / "flowlab/web/app.js").read_text()
        cls.css = (root / "flowlab/web/app.css").read_text()

    def test_form_present(self):
        self.assertIn('id="mixer-research-form"', self.html)

    def test_results_present(self):
        self.assertIn('id="mixer-research-results"', self.html)
        self.assertIn('id="mixer-research-metrics"', self.html)
        self.assertIn('id="mixer-research-scenarios"', self.html)

    def test_configuration_controls(self):
        for name in (
            "seeds",
            "transaction_count",
            "max_inputs",
            "single_owner",
            "collaborative",
            "mixed_ownership",
            "matched_input_profile",
        ):
            self.assertIn(f'name="{name}"', self.html)

    def test_existing_mixer_controls_preserved(self):
        for element_id in (
            "mixer-pool-form",
            "mixer-session-form",
            "mixer-registration-form",
            "mixer-address-form",
            "mixer-sessions",
        ):
            self.assertIn(f'id="{element_id}"', self.html)

    def test_authenticated_action_used(self):
        self.assertIn(
            'api("mixer-research-run", request)', self.js
        )

    def test_readonly_guard(self):
        self.assertIn(
            'form.hidden = true;', self.js
        )

    def test_safe_text_rendering(self):
        self.assertIn(
            'number.textContent = metricText(value);', self.js
        )
        self.assertIn(
            'heading.textContent = title;', self.js
        )

    def test_frontend_styles_present(self):
        self.assertIn(
            ".mixer-research-panel {", self.css
        )
        self.assertIn(
            ".mixer-research-metrics {", self.css
        )

    def test_single_initialization(self):
        self.assertEqual(
            self.js.count("function initMixerResearchForm("), 1
        )


if __name__ == "__main__":
    unittest.main()
