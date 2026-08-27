"""
DeterministicAuditService
=========================
A lightweight, regex-based numeric consistency checker.

Only runs when one section has role 'methodology' and the other has role 'results'.
Does NOT call Gemini — fully deterministic and synchronous.
"""
import re
import logging
from typing import List, Dict, Any, Optional

logger = logging.getLogger("resync.deterministic")

# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------
SAMPLE_SIZE_PATTERNS = [
    r"\bn\s*=\s*(\d+)",
    r"\bsample size\s*(?:of|was|is|:)?\s*(\d+)",
]
PERCENTAGE_PATTERN = r"\b(\d+(?:\.\d+)?)\s*%"

PVALUE_PATTERN = re.compile(
    r"\bp[\s-]*(?:value)?\s*(<=|>=|≤|≥|=|<|>)\s*(\.\d+|\d+(?:\.\d+)?)",
    re.IGNORECASE,
)
CI_PATTERN = re.compile(
    r"(?:95%?\s*CI|confidence interval)\s*[:=]?\s*[\[\(]?\s*"
    r"(-?\d+(?:\.\d+)?)\s*(?:,|-|–|to)\s*(-?\d+(?:\.\d+)?)\s*[\]\)]?",
    re.IGNORECASE,
)
SIGNIFICANT_NEGATIVE_PATTERN = re.compile(
    r"\b(?:not|no)\s+(?:statistically\s+)?significant(?:ly)?\b"
    r"|\b(?:failed|fail)\s+to\s+reach\s+significance\b"
    r"|\bdid\s+not\s+reach\s+significance\b",
    re.IGNORECASE,
)
SIGNIFICANT_POSITIVE_PATTERN = re.compile(
    r"\b(?:statistically\s+)?significant(?:ly)?\b",
    re.IGNORECASE,
)

METHODOLOGY_ROLES = {"methodology"}
RESULTS_ROLES = {"results"}


class DeterministicAuditService:
    """
    Performs deterministic numeric consistency checks between two manuscript sections.
    Flags blatant mismatches in sample sizes and percentages, invalid p-values and
    confidence intervals, and significance-language contradictions — all without
    calling an LLM.
    """

    def audit_numeric_consistency(
        self,
        section_a: str,
        section_b: str,
        role_a: str,
        role_b: str,
    ) -> List[Dict[str, Any]]:
        """
        Compare numeric values between two sections.

        Only executes when one role is 'methodology' and the other is 'results'
        (in either order).

        Parameters
        ----------
        section_a, section_b : str
            Full text of the two sections being compared.
        role_a, role_b : str
            Standardized internal role strings (from ManuscriptParserService.normalize_role).

        Returns
        -------
        List[Dict[str, Any]]
            Each dict has keys: type, sections, value_type, expected, found,
            evidence_a, evidence_b.
            Returns an empty list if the role pair does not qualify or no
            mismatches are detected.
        """
        roles = {role_a.lower(), role_b.lower()}
        if not (roles & METHODOLOGY_ROLES and roles & RESULTS_ROLES):
            logger.debug(
                "DeterministicAuditService: Skipping pair (%s, %s) — not methodology/results.",
                role_a, role_b
            )
            return []

        # Determine which section is methodology and which is results
        if role_a.lower() in METHODOLOGY_ROLES:
            method_text, result_text = section_a, section_b
            method_label, result_label = "methodology", "results"
        else:
            method_text, result_text = section_b, section_a
            method_label, result_label = "results", "methodology"

        flags: List[Dict[str, Any]] = []

        # -----------------------------------------------------------------------
        # Check 1: Sample sizes
        # -----------------------------------------------------------------------
        method_samples = self._extract_sample_sizes(method_text)
        result_samples = self._extract_sample_sizes(result_text)

        if method_samples and result_samples:
            for m_val, m_evidence in method_samples:
                for r_val, r_evidence in result_samples:
                    if abs(m_val - r_val) >= 1:  # exact value mismatch
                        flags.append({
                            "type": "deterministic_numeric_mismatch",
                            "sections": [method_label, result_label],
                            "value_type": "sample_size",
                            "expected": m_val,
                            "found": r_val,
                            "evidence_a": m_evidence,
                            "evidence_b": r_evidence,
                        })

        # -----------------------------------------------------------------------
        # Check 2: Percentages
        # -----------------------------------------------------------------------
        method_pcts = self._extract_percentages(method_text)
        result_pcts = self._extract_percentages(result_text)

        if method_pcts and result_pcts:
            for m_val, m_evidence in method_pcts:
                for r_val, r_evidence in result_pcts:
                    if abs(m_val - r_val) > 5.0:  # > 5 percentage points
                        flags.append({
                            "type": "deterministic_numeric_mismatch",
                            "sections": [method_label, result_label],
                            "value_type": "percentage",
                            "expected": m_val,
                            "found": r_val,
                            "evidence_a": m_evidence,
                            "evidence_b": r_evidence,
                        })

        # -----------------------------------------------------------------------
        # Check 3: P-value validity and significance-language consistency
        # -----------------------------------------------------------------------
        for text, label in ((method_text, method_label), (result_text, result_label)):
            for operator, value, sentence in self._extract_p_values(text):
                if value < 0 or value > 1:
                    flags.append({
                        "type": "deterministic_invalid_value",
                        "sections": [label],
                        "value_type": "p_value",
                        "found": value,
                        "evidence_a": sentence,
                    })
                    continue  # out-of-range p-values can't be checked for significance

                claim = self._classify_significance_claim(sentence)
                if claim is None:
                    continue
                implied = self._infer_significance(operator, value)
                if implied is None or implied == claim:
                    continue
                flags.append({
                    "type": "deterministic_significance_mismatch",
                    "sections": [label],
                    "value_type": "p_value",
                    "p_value": value,
                    "claimed_significance": "significant" if claim else "not significant",
                    "implied_significance": "significant" if implied else "not significant",
                    "evidence_a": sentence,
                })

        # -----------------------------------------------------------------------
        # Check 4: Confidence interval bound validity
        # -----------------------------------------------------------------------
        for text, label in ((method_text, method_label), (result_text, result_label)):
            for lower, upper, sentence in self._extract_confidence_intervals(text):
                if lower > upper:
                    flags.append({
                        "type": "deterministic_invalid_value",
                        "sections": [label],
                        "value_type": "confidence_interval",
                        "found": f"[{lower}, {upper}]",
                        "evidence_a": sentence,
                    })

        if flags:
            logger.info(
                "DeterministicAuditService: %d numeric mismatch(es) found between '%s' and '%s'.",
                len(flags), role_a, role_b
            )

        return flags

    # ---------------------------------------------------------------------------
    # Private helpers
    # ---------------------------------------------------------------------------

    @staticmethod
    def _extract_sample_sizes(text: str) -> List[tuple]:
        """
        Extracts (integer_value, surrounding_sentence) tuples for sample sizes.
        Only returns distinct values.
        """
        results = []
        seen: set = set()
        sentences = re.split(r'(?<=[.!?])\s+', text)
        for pattern in SAMPLE_SIZE_PATTERNS:
            for sentence in sentences:
                for match in re.finditer(pattern, sentence, re.IGNORECASE):
                    val = int(match.group(1))
                    if val not in seen:
                        seen.add(val)
                        results.append((val, sentence.strip()))
        return results

    @staticmethod
    def _extract_percentages(text: str) -> List[tuple]:
        """
        Extracts (float_value, surrounding_sentence) tuples for percentages.
        Only returns distinct values.
        """
        results = []
        seen: set = set()
        sentences = re.split(r'(?<=[.!?])\s+', text)
        for sentence in sentences:
            for match in re.finditer(PERCENTAGE_PATTERN, sentence):
                val = float(match.group(1))
                if val not in seen:
                    seen.add(val)
                    results.append((val, sentence.strip()))
        return results

    @staticmethod
    def _extract_p_values(text: str) -> List[tuple]:
        """
        Extracts (operator, float_value, surrounding_sentence) tuples for
        reported p-values, e.g. 'p < 0.05', 'p = .032', 'p-value = 0.01'.
        Only returns distinct (operator, value) combinations.
        """
        results = []
        seen: set = set()
        sentences = re.split(r'(?<=[.!?])\s+', text)
        for sentence in sentences:
            for match in PVALUE_PATTERN.finditer(sentence):
                operator = match.group(1)
                val = float(match.group(2))
                key = (operator, val)
                if key not in seen:
                    seen.add(key)
                    results.append((operator, val, sentence.strip()))
        return results

    @staticmethod
    def _extract_confidence_intervals(text: str) -> List[tuple]:
        """
        Extracts (lower_bound, upper_bound, surrounding_sentence) tuples for
        reported confidence intervals, e.g. '95% CI [1.2, 3.4]', '95% CI: 1.2-3.4'.
        Only returns distinct (lower, upper) combinations.
        """
        results = []
        seen: set = set()
        sentences = re.split(r'(?<=[.!?])\s+', text)
        for sentence in sentences:
            for match in CI_PATTERN.finditer(sentence):
                lower = float(match.group(1))
                upper = float(match.group(2))
                key = (lower, upper)
                if key not in seen:
                    seen.add(key)
                    results.append((lower, upper, sentence.strip()))
        return results

    @staticmethod
    def _classify_significance_claim(sentence: str) -> Optional[bool]:
        """
        Returns True if the sentence claims a significant result, False if it
        claims a non-significant result, or None if it makes no such claim.
        Negative phrasing ('not significant') is checked first since it would
        otherwise also match the positive pattern.
        """
        if SIGNIFICANT_NEGATIVE_PATTERN.search(sentence):
            return False
        if SIGNIFICANT_POSITIVE_PATTERN.search(sentence):
            return True
        return None

    @staticmethod
    def _infer_significance(operator: str, value: float) -> Optional[bool]:
        """
        Determines whether a reported p-value/threshold pair unambiguously
        implies statistical significance (p < 0.05) or non-significance.
        Returns None when the operator/value combination is ambiguous
        (e.g. 'p < 0.10' does not confirm or rule out p < 0.05).
        """
        op = operator.strip()
        if op == "=":
            return value < 0.05
        if op in ("<", "<=", "≤"):
            return True if value <= 0.05 else None
        if op in (">", ">=", "≥"):
            return False if value >= 0.05 else None
        return None


# Module-level singleton
deterministic_service = DeterministicAuditService()
