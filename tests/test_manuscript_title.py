import pytest

from services.manuscript_title import TITLE_LIMIT, derive_title


PAPAIA_FRONT_PAGE = "i\nPAPAIA: AN AI-POWERED SYSTEM FOR\nPAPAYA DISEASE IDENTIFICATION\nA Capstone Research Proposal\nPresented to the Faculty\n"


@pytest.mark.parametrize("text,expected", [
    (PAPAIA_FRONT_PAGE, "PAPAIA: AN AI-POWERED SYSTEM FOR PAPAYA DISEASE IDENTIFICATION"),
    ("Effects of Fertilizer on Rice Yield\n\nJuan Dela Cruz\n", "Effects of Fertilizer on Rice Yield"),
    ("\n\n   \nEffects of Fertilizer on Rice Yield\nBody text starts here.", "Effects of Fertilizer on Rice Yield"),
    ("ii\n\nTable of Contents\n1. Papaya Detection\n", "Papaya Detection"),
    ("# **Papaya Detection**\n\nBody.", "Papaya Detection"),
    ("12\n- 3 -\nA\nPapaya Detection with Deep Learning\n", "Papaya Detection with Deep Learning"),
    ("[Papaya Detection](https://example.com) for Farms\n", "Papaya Detection for Farms"),
    ("\n \n\t\n", None),
])
def test_derive_title_from_meaningful_lines(text, expected):
    assert derive_title(text) == expected


def test_title_is_capped_at_limit():
    long_line = "WORD " * 80
    title = derive_title(long_line)
    assert len(title) <= TITLE_LIMIT
    assert title.startswith("WORD WORD")
