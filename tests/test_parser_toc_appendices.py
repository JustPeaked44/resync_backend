import unittest

from services.parser import ManuscriptParserService


class TestParserTocAppendices(unittest.TestCase):
    def test_toc_appendices_does_not_hide_manuscript_sections(self):
        text = """Objectives of the Study
5
References
92
APPENDICES
97
Objectives of the Study
Identify papaya leaf diseases from images.
References
Author, A. (2024). Papaya disease research.
Appendices
Supplemental material must be ignored.
"""

        result = ManuscriptParserService.parse_manuscript_sections(
            text, ["Objectives of the Study", "References"]
        )

        self.assertIn("Identify papaya leaf diseases", result["parsed_sections"]["Objectives of the Study"])
        self.assertIn("Papaya disease research", result["parsed_sections"]["References"])
        self.assertNotIn("Supplemental material", result["parsed_sections"]["References"])


if __name__ == "__main__":
    unittest.main()
