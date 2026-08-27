import re
import spacy
from typing import Dict, List, Any, Optional

# Load spaCy English NLP model
try:
    nlp = spacy.load("en_core_web_sm")
except OSError:
    import spacy.cli
    spacy.cli.download("en_core_web_sm")
    nlp = spacy.load("en_core_web_sm")

# Default Capstone TOC Template
DEFAULT_TOC_TEMPLATE = [

  "Title",
  "Rationale of the Study",
  "Objectives of the Study",
  "Scope and Limitation of the Study",
  "Significance of the Study",
  "Flow of the Study",
  "Definition of Terms",
  "Theoretical Background",
  "Related Literature",
  "Related Studies",
  "Comparative Matrix",
  "Software Engineering Methodology",
  "Business Model Canvas",
  "Program Workflow",
  "Validation Board",
  "Gantt Chart",
  "Functional Decomposition Diagram",
  "Use Case Diagram",
  "User Interface Design",
  "Storyboard",
  "Entity Relationship Diagram",
  "Data Dictionary",
  "Network Model",
  "Network Topology",
  "Technology Stack Diagram",
  "Software Specification",
  "Hardware Specification",
  "Program Specification",
  "List of Modules",
  "Unit Testing",
  "Integration testing",
  "Alpha Acceptance Testing",
  "Deployment Diagram",
  "Implementation Budget",
  "User Guide",
  "Project Schedule",
  "Conclusion",
  "Recommendations",
  "References",
  "Appendices"
]

# ---------------------------------------------------------------------------
# Role Normalization Mapping
# Maps lowercase heading keywords → standardized internal role strings.
# Used by normalize_role() to canonicalize user-submitted TOC headings.
# ---------------------------------------------------------------------------
ROLE_MAPPING: dict = {
    # abstract
    "abstract":                          "abstract",
    "executive summary":                 "abstract",

    # introduction
    "introduction":                      "introduction",
    "background":                        "introduction",
    "rationale":                         "introduction",
    "rationale of the study":            "introduction",

    # objectives
    "objectives":                        "objectives",
    "aim":                               "objectives",
    "aims":                              "objectives",
    "purpose":                           "objectives",
    "statement of the problem":          "objectives",
    "research questions":                "objectives",
    "goals":                             "objectives",
    "objectives of the study":           "objectives",

    # methodology
    "method":                            "methodology",
    "methods":                           "methodology",
    "methodology":                       "methodology",
    "procedure":                         "methodology",
    "design":                            "methodology",
    "materials":                         "methodology",
    "materials and methods":             "methodology",
    "study design":                      "methodology",
    "approach":                          "methodology",
    "software engineering methodology":  "methodology",

    # results
    "result":                            "results",
    "results":                           "results",
    "finding":                           "results",
    "findings":                          "results",
    "data analysis":                     "results",
    "presentation":                      "results",
    "results and discussion":            "results",
    "results and analysis":              "results",

    # discussion
    "discussion":                        "discussion",
    "interpretation":                    "discussion",
    "analysis of results":               "discussion",
    "discussion of results":             "discussion",
    "discussion of findings":            "discussion",

    # conclusion
    "conclusion":                        "conclusion",
    "conclusions":                       "conclusion",
    "summary":                           "conclusion",
    "recommendation":                    "conclusion",
    "recommendations":                   "conclusion",

    # references
    "references":                        "references",
    "bibliography":                      "references",

    # limitations
    "limitation":                        "limitations",
    "limitations":                       "limitations",
    "scope and limitation":              "limitations",
    "scope and limitation of the study": "limitations",

    # future_work
    "future work":                       "future_work",
    "future research":                   "future_work",
    "recommendations for future":        "future_work",
}

class ManuscriptParserService:
    """
    Uses spaCy NLP to parse raw manuscript text and segment it into 
    structured sections matching the mandatory Department Template TOC.
    """

    @classmethod
    def parse_manuscript_sections(
        cls,
        text: str,
        template_toc: List[str] = None
    ) -> Dict[str, Any]:
        """
        Segments raw manuscript text into structured section blocks.

        If `template_toc` is provided (and contains real content after
        filtering placeholder junk), sections are parsed against that
        template and keyed by the original template heading strings — this
        is the existing TOC-driven path. If `template_toc` is None (omitted
        entirely by the caller), "scan and go" auto-detection is used
        instead (see auto_detect_sections()): sections are detected
        heuristically from the document itself and keyed by canonical role.

        Detects missing mandatory sections based on the template
        (TOC-driven path) or against a minimal required role set
        (auto-detect path).

        Args:
            text: Raw manuscript text to parse.
            template_toc: Optional ordered list of mandatory section
                headings. Pass None to trigger auto-detection instead of
                requiring a template.

        Returns:
            Dict with keys: "parsed_sections", "missing_sections",
            "has_all_required_sections", "total_sections_count",
            "section_roles", "auto_detected", "detection_confidence".
        """
        # Remember whether the caller omitted the TOC entirely, before the
        # junk-filtering step below reassigns template_toc. Only a genuine
        # omission routes to auto-detection; a TOC that filters down to
        # empty (e.g. Swagger's ["string"] placeholder) still falls back to
        # the default template, preserving existing behavior.
        toc_was_omitted = template_toc is None

        # Filter out Swagger UI default placeholder strings like ["string"]
        if template_toc:
            template_toc = [
                s.strip() for s in template_toc
                if s and s.strip().lower() not in ["string", "none", "null", ""]
            ]

        if toc_was_omitted:
            # --- "Scan and go" auto-detection path (no template supplied) ---
            detection = cls.auto_detect_sections(text)
            parsed_sections = detection["sections"]
            # Roles are already normalized keys, so section_roles is a
            # self-mapping — keeps parsed_sections/section_roles key-consistent
            # for downstream consumers (embedding/coherence/deterministic checks).
            section_roles: Dict[str, str] = {role: role for role in parsed_sections}

            required_roles = {"introduction", "methodology", "results", "discussion"}
            missing_sections = [
                role for role in required_roles
                if role not in parsed_sections or len(parsed_sections[role]) < 20
            ]

            return {
                "parsed_sections": parsed_sections,
                "missing_sections": missing_sections,
                "has_all_required_sections": len(missing_sections) == 0,
                "total_sections_count": len(parsed_sections),
                "section_roles": section_roles,
                "auto_detected": True,
                "detection_confidence": detection["confidence"],
            }

        # Fallback to default TOC template if list is empty or invalid
        if not template_toc or len(template_toc) == 0:
            template_toc = DEFAULT_TOC_TEMPLATE

        # 1. Split document by non-empty lines
        lines = [line.strip() for line in text.split("\n") if line.strip()]

        parsed_sections_lines: Dict[str, List[str]] = {section: [] for section in template_toc}
        current_section = template_toc[0]  # Dynamically set initial section


        # 2. Iterate line-by-line and match headings
        for line in lines:
            matched_header = cls._match_header(line, template_toc)
            if matched_header:
                current_section = matched_header
            elif line.strip().lower() in ["appendices", "appendix", "curriculum vitae"]:
                current_section = None
            else:
                if current_section and current_section in parsed_sections_lines:
                    parsed_sections_lines[current_section].append(line)

        # 3. Join with newlines (a space-joined blob collapses a multi-entry
        # References list into one line, which downstream reference
        # segmentation can't split) and clean up leading/trailing whitespace
        parsed_sections = {k: "\n".join(v).strip() for k, v in parsed_sections_lines.items()}

        # 4. Identify missing mandatory sections (< 20 characters)
        missing_sections = [
            s for s, content in parsed_sections.items()
            if len(content) < 20 and s.lower() != "title"
        ]

        # 5. Build section_roles: map each heading → standardized internal role
        section_roles: Dict[str, str] = {
            heading: cls.normalize_role(heading)
            for heading in parsed_sections.keys()
        }

        return {
            "parsed_sections": parsed_sections,
            "missing_sections": missing_sections,
            "has_all_required_sections": len(missing_sections) == 0,
            "total_sections_count": len(template_toc),
            "section_roles": section_roles,  # heading → standardized role
            "auto_detected": False,
            "detection_confidence": 1.0,
        }

    @classmethod
    def auto_detect_sections(cls, text: str) -> Dict[str, Any]:
        """
        Detects academic section headings in raw manuscript text without a
        template TOC ("scan and go" mode), mapping each detected heading to
        a canonical role via ROLE_MAPPING.

        Heading candidates are identified line-by-line using heuristics:
        short lines (<80 chars), no trailing period, and a remainder (after
        stripping chapter/section/numbering prefixes) that matches a known
        ROLE_MAPPING keyword. Lines that don't resolve to a role are treated
        as body text of whichever section is currently open.

        A deterministic confidence score (0.0-1.0) is computed from how many
        core/extra academic roles were found and whether they appeared in a
        plausible reading order. This method makes no external model calls;
        detection is purely regex/heuristic-based, matching the rest of this
        module's lightweight, reproducible design.

        Args:
            text: Raw manuscript text to scan.

        Returns:
            Dict with keys:
                "sections": Dict[str, str] mapping role -> accumulated body
                    text for that role.
                "confidence": float in [0.0, 1.0] estimating how reliable
                    the auto-detection is for this document.
                "detected_headings": List[str] of the raw heading lines
                    found, in document order.
        """
        core_roles = ("introduction", "methodology", "results", "discussion")
        extra_roles = ("abstract", "objectives", "conclusion", "references")
        canonical_order = [
            "abstract", "introduction", "objectives", "methodology",
            "results", "discussion", "conclusion", "references",
        ]
        prefix_re = re.compile(
            r"(?i)^(chapter\s+[ivxlcdm\d]+[\:\.-]?|section\s+[\d\.]+|[\d\.]+)\s*"
        )

        lines = [line.strip() for line in text.split("\n") if line.strip()]

        sections_lines: Dict[str, List[str]] = {}
        detected_headings: List[str] = []
        detected_roles_in_order: List[str] = []
        current_role: Optional[str] = None

        # 1. Walk lines, classifying each as a heading (role-mapped) or body text
        for line in lines:
            role = cls._detect_heading_role(line, prefix_re)
            if role:
                current_role = role
                if role not in sections_lines:
                    sections_lines[role] = []
                detected_headings.append(line)
                detected_roles_in_order.append(role)
            elif current_role is not None:
                sections_lines[current_role].append(line)

        # 2. Join with newlines (see parse_manuscript_sections for why: a
        # space-joined blob collapses a multi-entry References list into
        # one line) and clean up leading/trailing whitespace
        sections = {k: "\n".join(v).strip() for k, v in sections_lines.items()}

        # 3. Confidence calculation (deterministic, additive/subtractive)
        confidence = 0.0
        if detected_headings:
            confidence += 0.2

        found_roles = set(detected_roles_in_order)
        for role in core_roles:
            if role in found_roles:
                confidence += 0.2
        for role in extra_roles:
            if role in found_roles:
                confidence += 0.05

        # Order check: sequence of canonical indices for detected roles that
        # are part of the canonical order, in the order they were detected.
        # If that index sequence is not non-decreasing anywhere, the
        # document's section order looks wrong — apply the penalty once.
        order_indices = [
            canonical_order.index(role)
            for role in detected_roles_in_order
            if role in canonical_order
        ]
        order_is_wrong = any(
            order_indices[i] > order_indices[i + 1]
            for i in range(len(order_indices) - 1)
        )
        if order_is_wrong:
            confidence -= 0.3

        confidence = max(0.0, min(1.0, confidence))

        return {
            "sections": sections,
            "confidence": confidence,
            "detected_headings": detected_headings,
        }

    @classmethod
    def _detect_heading_role(cls, line: str, prefix_re: "re.Pattern") -> Optional[str]:
        """
        Determines whether `line` is an academic heading candidate and, if
        so, which canonical role it maps to.

        A line is a heading candidate if it is short (<80 chars), does not
        end with a period, and — after stripping a leading chapter/section/
        numbering prefix — its remainder matches a ROLE_MAPPING keyword via
        _best_role_match(). A numbering prefix alone (e.g. "2.1" or
        "CHAPTER II") with no recognizable keyword on the same line is not
        sufficient to assign a role; such lines are treated as body text by
        the caller.

        Args:
            line: A single stripped, non-empty line of manuscript text.
            prefix_re: Compiled regex for stripping chapter/section/
                numbering prefixes (mirrors the pattern used by
                normalize_role and _match_header).

        Returns:
            The matched role string, or None if the line is not a
            recognizable heading.
        """
        if len(line) >= 80 or line.rstrip().endswith("."):
            return None

        cleaned = prefix_re.sub("", line).strip().lower()
        # Remove any remaining leading non-alpha characters
        cleaned = re.sub(r"^[^a-z]+", "", cleaned).strip()

        if not cleaned:
            return None

        return cls._best_role_match(cleaned)

    @staticmethod
    def _best_role_match(cleaned: str) -> Optional[str]:
        """
        Finds the best ROLE_MAPPING match for an already-cleaned heading
        string.

        Searches ROLE_MAPPING for keywords that equal, contain, or are
        contained by `cleaned`. An exact keyword match always wins (it is
        unambiguous); otherwise the longest matching keyword wins (most
        specific partial match). This exact-match priority matters for
        short, common headings like "Discussion" or "Results": without it,
        a longer superstring keyword such as "results and discussion"
        (mapped to "results") would incorrectly outrank the exact
        "discussion" keyword purely by character count. Shared by
        normalize_role() (TOC-heading path) and _detect_heading_role()
        (auto-detect path) so both stay consistent.

        Args:
            cleaned: A lowercased, prefix-stripped heading string.

        Returns:
            The best-matching role string, or None if no ROLE_MAPPING
            keyword matches.
        """
        # Exact match first: unambiguous and should never lose to a
        # longer superstring keyword.
        if cleaned in ROLE_MAPPING:
            return ROLE_MAPPING[cleaned]

        matches = [
            (keyword, role)
            for keyword, role in ROLE_MAPPING.items()
            if keyword in cleaned or cleaned in keyword
        ]

        if not matches:
            return None

        # Longest matching keyword wins (most specific)
        best_keyword, best_role = max(matches, key=lambda x: len(x[0]))
        return best_role

    @staticmethod
    def normalize_role(heading: str) -> str:
        """
        Normalizes a raw TOC heading string to a standardized internal role key.

        Steps:
        1. Strip leading numbers / punctuation (e.g., "1. Methodology" → "methodology").
        2. Lowercase and clean.
        3. Match against ROLE_MAPPING keys via the shared _best_role_match()
           helper. If multiple match, the longest keyword wins (most
           specific match wins).
        4. If no match, log a warning and return the cleaned heading unchanged.

        Returns one of the standard roles defined in ROLE_MAPPING, or the cleaned
        heading string if no mapping exists.
        """
        import logging as _logging
        _logger = _logging.getLogger("resync.parser")

        if not heading:
            return ""

        # Step 1 & 2: clean heading
        cleaned = re.sub(
            r"(?i)^(chapter\s+[ivxlcdm\d]+[\:\.-]?|section\s+[\d\.]+|[\d\.]+)\s*",
            "",
            heading.strip()
        ).strip().lower()
        # Remove any remaining leading non-alpha characters
        cleaned = re.sub(r"^[^a-z]+", "", cleaned).strip()

        # Step 3: find best matching ROLE_MAPPING key via shared helper
        best_role = ManuscriptParserService._best_role_match(cleaned)

        if best_role is None:
            _logger.warning(
                "normalize_role: No role mapping found for heading %r (cleaned: %r). Retaining original.",
                heading, cleaned
            )
            return cleaned  # retain cleaned heading if no match

        return best_role

    @staticmethod
    def _match_header(line: str, template_toc: List[str]) -> str:
        """
        Matches a line of text against expected template TOC headers.
        Ignores leading numbers/labels (e.g., "CHAPTER I: INTRODUCTION" -> "Introduction").
        Only matches if line is short (<= 10 words) to avoid catching body sentences.
        """
        # Guard: Heading lines are short (<= 10 words / 80 characters)
        if len(line.split()) > 10 or len(line) > 80:
            return None

        # Strip leading chapter/section labels (e.g., "CHAPTER I: INTRODUCTION" -> "introduction")
        clean_line = re.sub(r"(?i)^(chapter\s+[ivxlcdm\d]+[\:\.-]?|section\s+[\d\.]+|[\d\.]+)\s*", "", line).strip().lower()
        
        for header in template_toc:
            clean_header = header.strip().lower()
            # Exact match or prefix match
            if clean_line == clean_header or clean_line.startswith(clean_header):
                return header
        return None