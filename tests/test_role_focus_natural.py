import unittest

from role_focus import extract_role_focus


class NaturalRoleFocusTests(unittest.TestCase):
    def test_gtm_compound_signals_render_as_natural_list(self):
        job = {
            "job_title": "Remote GTM Engineer",
            "job_description": (
                "Build outbound infrastructure and sequencing. "
                "Own revenue reporting and pipeline reporting."
            ),
        }
        result = extract_role_focus(job, "GTM Engineer")
        self.assertEqual(
            result.text,
            "outbound infrastructure, sequencing, and revenue reporting",
        )
        self.assertEqual(result.text.count(" and "), 1)

    def test_ai_compound_signals_render_as_natural_list(self):
        job = {
            "job_title": "AI Engineer",
            "job_description": (
                "Build production AI systems, AI agents, agent workflows, "
                "and machine-learning model development."
            ),
        }
        result = extract_role_focus(job, "AI Engineer")
        self.assertEqual(
            result.text,
            "production AI systems, AI agents, workflow automation, and machine-learning model development",
        )
        self.assertEqual(result.text.count(" and "), 1)

    def test_fallback_copy_remains_unchanged(self):
        result = extract_role_focus(
            {"job_title": "GTM Engineer", "job_description": "General responsibilities."},
            "GTM Engineer",
        )
        self.assertEqual(
            result.text,
            "GTM systems, workflow automation, and revenue operations",
        )
        self.assertEqual(result.quality, "manual_required")


if __name__ == "__main__":
    unittest.main()
