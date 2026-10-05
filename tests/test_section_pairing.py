import unittest
from services.parser import ManuscriptParserService
from services.scoring import ROLE_PAIR_WEIGHTS, ROLE_PAIR_RATIONALE


class TestSectionPairing(unittest.TestCase):
    def test_normalize_role_panelist_mandated_headings(self):
        """Verify normalize_role canonicalizes key academic headings as requested by panelist."""
        self.assertEqual(ManuscriptParserService.normalize_role("Background of the Study"), "introduction")
        self.assertEqual(ManuscriptParserService.normalize_role("Statement of the Problem"), "objectives")
        self.assertEqual(ManuscriptParserService.normalize_role("Review of Related Literature"), "literature")
        self.assertEqual(ManuscriptParserService.normalize_role("Review of Related Literature & Studies"), "literature")
        self.assertEqual(ManuscriptParserService.normalize_role("Related Literature"), "literature")
        self.assertEqual(ManuscriptParserService.normalize_role("Literature Review"), "literature")

    def test_canonical_role_pair_weights_snapshot(self):
        """Guards against accidental modification of approved coherence weights."""
        expected = [
            ("objectives", "methodology", 0.22),
            ("methodology", "results", 0.20),
            ("results", "discussion", 0.16),
            ("objectives", "conclusion", 0.16),
            ("introduction", "objectives", 0.12),
            ("abstract", "__document__", 0.08),
            ("discussion", "conclusion", 0.06),
        ]
        self.assertEqual(ROLE_PAIR_WEIGHTS, expected)

    def test_role_pair_rationale_completeness(self):
        """Verifies every evaluated role pair is explicitly documented in ROLE_PAIR_RATIONALE."""
        scored_pairs = {(role_a, role_b) for role_a, role_b, _ in ROLE_PAIR_WEIGHTS}
        rationale_pairs = set(ROLE_PAIR_RATIONALE.keys())
        self.assertEqual(scored_pairs, rationale_pairs)
        for pair, explanation in ROLE_PAIR_RATIONALE.items():
            self.assertIsInstance(explanation, str)
            self.assertGreater(len(explanation.strip()), 10)


if __name__ == "__main__":
    unittest.main()
