import re
import threading
from typing import Dict, List, Any, Optional

_nlp_lock = threading.Lock()


def __getattr__(name):
    if name != "nlp":
        raise AttributeError(name)
    # The legacy AI text service imports this pipeline; parsing does not use it.
    with _nlp_lock:
        if "nlp" not in globals():
            import spacy
            pipeline = spacy.blank("en")
            pipeline.add_pipe("sentencizer")
            globals()["nlp"] = pipeline
        return globals()["nlp"]

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
    "background of the study":           "introduction",
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
    "research methodology":              "methodology",

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
    "scope and limitations of the study": "limitations",

    # future_work
    "future work":                       "future_work",
    "future research":                   "future_work",
    "recommendations for future":        "future_work",

    # literature
    "review of related literature":      "literature",
    "review of related literature & studies": "literature",
    "related literature":                "literature",
    "related studies":                   "literature",
    "review of literature":              "literature",
    "literature review":                 "literature",
}

class ManuscriptParserService:
    """
    Parses raw manuscript text and segments it into
    structured sections matching the mandatory Department Template TOC.
    """

    @classmethod
    def parse_manuscript_sections(
        cls,
        text: str,
        template_toc: List[str] = None,
        source_records: List[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Segments raw manuscript text into structured section blocks.

        If `template_toc` is provided (and contains real content after
        filtering placeholder junk), sections are parsed against that
        template and keyed by the original template heading strings — this
        is the existing TOC-driven path. Recognized academic headings absent
        from the template are retained under their manuscript heading names.
        If `template_toc` is None (omitted
        entirely by the caller), "scan and go" auto-detection is used
        instead (see auto_detect_sections()): sections are detected
        heuristically from the document itself and keyed by source-ordered
        occurrence names; `section_roles` maps those names to canonical roles.

        Args:
            text: Raw manuscript text to parse.
            template_toc: Optional ordered list of mandatory section
                headings. Pass None to trigger auto-detection instead of
                requiring a template.

        Returns:
            Dict with keys: "parsed_sections", "total_sections_count",
            "section_roles", "auto_detected", "detection_confidence",
            "section_order".
        """
        result = cls._parse_manuscript_sections(text, template_toc)
        if source_records:
            cls._attach_preview(result, source_records)
        return result

    @classmethod
    def _attach_preview(cls, result, records):
        """Align source records with parsed body lines; omit blocks when alignment is uncertain."""
        from bisect import bisect_left
        from services.ingestion import DocumentIngestionService

        lines = []
        for record in records:
            cleaned = DocumentIngestionService.strip_document_noise(record["text"])
            for index, line in enumerate(cleaned.splitlines()):
                if line.strip():
                    lines.append((line.strip(), record if index == 0 else None))
        headings, positions, variants = {}, {}, {}
        for index, (line, record) in enumerate(lines):
            headings.setdefault(line.casefold(), []).append(index)
            positions.setdefault(line, []).append(index)
            cells = tuple(record["cells"]) if record and record.get("cells") is not None else None
            variants.setdefault(line, []).append(cells)
        ambiguous_suffix = {}
        for line, values in variants.items():
            seen = set()
            flags = [False] * len(values)
            for index in range(len(values) - 1, -1, -1):
                if len(seen) < 2:
                    seen.add(values[index])
                flags[index] = len(seen) > 1
            ambiguous_suffix[line] = flags
        cursor = 0
        display_names, blocks_by_section, ambiguous_bases = dict(result.get("display_names") or {}), {}, set()
        for name in result.get("section_order", []):
            body = result["parsed_sections"].get(name, "")
            body_lines = [line.strip() for line in body.splitlines() if line.strip()]
            if not body_lines:
                continue
            heading = (display_names.get(name) or name).splitlines()[0].casefold()
            matches = headings.get(heading, [])
            heading_index = bisect_left(matches, cursor)
            search_cursor = matches[heading_index] + 1 if heading_index < len(matches) else cursor
            selected_indices = []
            ambiguous = False
            for body_line in body_lines:
                candidates = positions.get(body_line, [])
                found = bisect_left(candidates, search_cursor)
                if found == len(candidates):
                    break
                if ambiguous_suffix[body_line][found]:
                    ambiguous = True
                selected_indices.append(candidates[found])
                search_cursor = candidates[found] + 1
            if len(selected_indices) != len(body_lines):
                continue
            display_names.setdefault(name, name)
            selected = [lines[index] for index in selected_indices]
            cursor = search_cursor
            if ambiguous:
                ambiguous_bases.add(re.sub(r" \(\d+\)$", "", name))
                continue
            if not any(record and record.get("cells") is not None for _, record in selected):
                continue
            blocks = []
            table_id = None
            for line, record in selected:
                if record and record.get("cells") is not None:
                    if not blocks or table_id != record["table"]:
                        blocks.append({"type": "table", "rows": []})
                    blocks[-1]["rows"].append(record["cells"])
                    table_id = record["table"]
                elif record is None:
                    if blocks and blocks[-1]["type"] == "paragraph":
                        blocks[-1]["text"] += "\n" + line
                    continue
                else:
                    table_id = None
                    if blocks and blocks[-1]["type"] == "paragraph":
                        blocks[-1]["text"] += "\n" + line
                    else:
                        blocks.append({"type": "paragraph", "text": line})
            blocks_by_section[name] = blocks
        blocks_by_section = {name: blocks for name, blocks in blocks_by_section.items()
                             if re.sub(r" \(\d+\)$", "", name) not in ambiguous_bases}
        result["display_names"] = display_names
        result["section_blocks"] = blocks_by_section

    @classmethod
    def _parse_manuscript_sections(cls, text, template_toc=None):
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
            detection = cls.detect_structure(text)
            parsed_sections = detection["sections"]
            # Keep each detected occurrence in source order. Downstream scoring
            # still groups these keys through section_roles when it needs roles.
            section_roles: Dict[str, str] = detection.get(
                "section_roles",
                {role: role for role in parsed_sections},
            )

            return {
                "parsed_sections": parsed_sections,
                "total_sections_count": len(parsed_sections),
                "section_roles": section_roles,
                "auto_detected": True,
                "structure": detection.get("structure", "headings"),
                "detection_confidence": detection["confidence"],
                "section_order": detection.get("section_order", list(parsed_sections)),
                "display_names": detection.get("display_names", {}),
            }

        # Fallback to default TOC template if list is empty or invalid
        if not template_toc or len(template_toc) == 0:
            template_toc = DEFAULT_TOC_TEMPLATE

        # 1. Split document by non-empty lines
        lines = [line.strip() for line in text.split("\n") if line.strip()]

        parsed_sections_lines: Dict[str, List[str]] = {section: [] for section in template_toc}
        current_section = template_toc[0]  # Dynamically set initial section
        matched_section_keys: List[str] = []


        # 2. Iterate line-by-line and match headings
        toc_line_re = re.compile(r"\.{3,}|\.{2,}\s*\d+\s*$|\s{3,}\d{1,3}\s*$")
        for line in lines:
            # Skip TOC-style "Heading . . . . 14" lines
            if toc_line_re.search(line):
                continue
            if line.strip().lower() in ["appendices", "appendix", "curriculum vitae"]:
                current_section = None
                continue
            matched_header = cls._match_header(line, template_toc)
            if matched_header:
                section_key = matched_header
                if section_key in parsed_sections_lines:
                    existing_content = "\n".join(parsed_sections_lines[section_key]).strip()
                    if len(existing_content) >= 150:
                        occurrence = 2
                        while f"{matched_header} ({occurrence})" in parsed_sections_lines:
                            occurrence += 1
                        section_key = f"{matched_header} ({occurrence})"
                parsed_sections_lines.setdefault(section_key, [])
                # If the section accumulated very little content so far it was
                # probably a TOC occurrence — reset and start fresh from here.
                if len("\n".join(parsed_sections_lines[section_key]).strip()) < 150:
                    parsed_sections_lines[section_key] = []
                current_section = section_key
                if section_key not in matched_section_keys:
                    matched_section_keys.append(section_key)
            else:
                if current_section and current_section in parsed_sections_lines:
                    parsed_sections_lines[current_section].append(line)

        # 3. Join with newlines (a space-joined blob collapses a multi-entry
        # References list into one line, which downstream reference
        # segmentation can't split) and clean up leading/trailing whitespace
        ordered_keys = matched_section_keys + [
            section for section in parsed_sections_lines if section not in matched_section_keys
        ]
        parsed_sections = {
            key: "\n".join(parsed_sections_lines[key]).strip()
            for key in ordered_keys
        }

        # 4. Identify missing mandatory sections (< 20 characters)
        # 5. Build section_roles: map each heading → standardized internal role
        section_roles: Dict[str, str] = {
            heading: cls.normalize_role(heading)
            for heading in parsed_sections.keys()
        }

        return {
            "parsed_sections": parsed_sections,
            "total_sections_count": len(parsed_sections),
            "section_roles": section_roles,  # heading → standardized role
            "auto_detected": False,
            "structure": "template",
            "detection_confidence": 1.0,
            "section_order": list(parsed_sections),
            "display_names": {name: name for name in parsed_sections},
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
                "sections": Dict[str, str] mapping each detected section
                    occurrence to its body text, in source order. Repeated
                    canonical roles receive a numbered suffix.
                "confidence": float in [0.0, 1.0] estimating how reliable
                    the auto-detection is for this document.
                "detected_headings": List[str] of the raw heading lines
                    found, in document order.
                "section_roles": Dict[str, str] mapping occurrence names to
                    canonical roles.
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
        heading_keys: Dict[str, List[str]] = {}
        section_roles: Dict[str, str] = {}
        display_names: Dict[str, str] = {}
        detected_headings: List[str] = []
        detected_roles_in_order: List[str] = []
        current_section_key: Optional[str] = None
        references_key: Optional[str] = None

        # 1. Walk lines, classifying each as a heading (role-mapped) or body text
        table_neighbors = {"test case description", "expected result", "actual result", "pre-condition",
                           "remarks", "critical", "number", "table name", "fk referenced table"}
        for index, line in enumerate(lines):
            previous = lines[index - 1].lower() if index else ""
            following = lines[index + 1].lower() if index + 1 < len(lines) else ""
            in_table = ((line.lower() in {"expected result", "actual result", "test results"}
                         and (previous in table_neighbors or following in table_neighbors))
                        or (line.lower() == "recommendation" and
                            (re.match(r"table\s+\d", previous) or previous == "fk referenced table")))
            role = None if in_table else cls._detect_heading_role(line, prefix_re)
            if role and references_key and role != "references":
                # Appendix headings and tables after the reference list are not manuscript sections.
                current_section_key = None
                continue
            if role == "references" and references_key and len(" ".join(sections_lines[references_key]).strip()) >= 150:
                current_section_key = references_key  # extra reference lists stay one section
                continue
            if role:
                heading = re.sub(r" \(\d+\)$", "", prefix_re.sub("", line).strip().lower())
                # If we already have this literal heading and its content so far is very
                # short (< 150 chars), it was likely a TOC entry — discard it
                # and restart accumulation from this real heading occurrence.
                previous_keys = heading_keys.get(heading, [])
                previous_key = previous_keys[-1] if previous_keys else None
                if previous_key and len("\n".join(sections_lines[previous_key]).strip()) < 150:
                    section_key = previous_key
                    sections_lines[section_key] = []
                else:
                    occurrence = len(previous_keys) + 1
                    section_key = heading if occurrence == 1 else f"{heading} ({occurrence})"
                    heading_keys.setdefault(heading, []).append(section_key)
                    sections_lines[section_key] = []
                section_roles[section_key] = role
                display_names[section_key] = line
                current_section_key = section_key
                if role == "references":
                    references_key = section_key
                detected_headings.append(line)
                detected_roles_in_order.append(role)
            elif current_section_key is not None:
                sections_lines[current_section_key].append(line)

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
            "section_roles": section_roles,
            "section_order": list(sections),
            "display_names": display_names,
        }

    # ------------------------------------------------------------------
    # Papers that do not use the usual capstone headings
    # ------------------------------------------------------------------
    MIN_USABLE_SECTIONS = 3        # fewer than this and the usual-heading result is not enough to compare anything
    MIN_COVERAGE = 0.25            # share of the paper's words the sections must hold
    MIN_SECTION_WORDS = 5
    MIN_WORDS_TO_SPLIT = 150

    @classmethod
    def _usable_words(cls, sections: Dict[str, str], roles: Dict[str, str]) -> tuple:
        usable = [len(body.split()) for name, body in sections.items()
                  if roles.get(name) != "references" and len(body.split()) >= cls.MIN_SECTION_WORDS]
        return len(usable), sum(usable)

    @classmethod
    def _enough(cls, detection: Dict[str, Any], total_words: int) -> bool:
        count, words = cls._usable_words(detection["sections"], detection.get("section_roles", {}))
        return count >= cls.MIN_USABLE_SECTIONS and words >= cls.MIN_COVERAGE * max(total_words, 1)

    @classmethod
    def detect_structure(cls, text: str) -> Dict[str, Any]:
        """
        Splits a paper into sections whatever its layout. Three layers, each used only when the one before it did not
        find enough to compare (see _enough): 1) the usual academic headings (auto_detect_sections), 2) headings found
        by their shape (numbered, Roman numeral, "Chapter N", ALL CAPS, short title lines), 3) equal parts in reading
        order. The result says which layer was used in "structure": "headings", "structure" or "split".
        """
        total_words = len(text.split())
        usual = cls.auto_detect_sections(text)
        if cls._enough(usual, total_words):
            return {**usual, "structure": "headings"}
        shaped = cls._detect_shaped_headings(text)
        if cls._enough(shaped, total_words):
            return {**shaped, "structure": "structure"}
        split = cls._split_into_parts(text)
        if split["sections"]:
            return {**split, "structure": "split"}
        # nothing better: keep whichever layer found the most, even if it is little
        best = max((usual, shaped), key=lambda found: cls._usable_words(found["sections"], found.get("section_roles", {}))[1])
        return {**best, "structure": "headings" if best is usual else "structure"}

    _CHAPTER_RE = re.compile(r"(?i)^(chapter|part|section|unit)\s+(?:[ivxlcdm]+|\d+|one|two|three|four|five|six|seven|eight|nine|ten)\b[\s:.\-–—]*(.*)$")
    _NUMBERED_RE = re.compile(r"^(?:\d+(?:\.\d+)*[.)]?|[IVXLC]+[.)]|[A-Z][.)])\s+(?=[A-Za-z])(.+)$")
    _NOT_HEADING_RE = re.compile(r"\.{3,}|\s\d{1,3}$|[|@]|https?://|^(?:figure|fig\.|table|chart|source|note)\b|^[*•☐☑☒\-]\s", re.I)

    @classmethod
    def _heading_text(cls, line: str, next_line: str) -> Optional[str]:
        """The cleaned heading name when `line` looks like a heading by its shape, else None."""
        if not 3 <= len(line) <= 90 or cls._NOT_HEADING_RE.search(line):
            return None
        if line.endswith((".", ",", ";", ":", "?", "!")) or line.count("(") != line.count(")"):
            return None
        words = line.split()
        next_words = len(next_line.split())
        chapter = cls._CHAPTER_RE.match(line)
        if chapter:
            return None if len(words) > 14 else (chapter.group(2).strip() or line)
        if len(words) > 12 or next_words < 6:
            return None
        numbered = cls._NUMBERED_RE.match(line)
        if numbered:
            title = numbered.group(1).strip()
            return title if title[:1].isupper() and len(title.split()) <= 10 else None
        letters = [c for c in line if c.isalpha()]
        if len(letters) >= 4 and all(c.isupper() for c in letters):
            return line
        capitalized = sum(word[:1].isupper() for word in words)
        if 1 <= len(words) <= 9 and line[:1].isupper() and next_words >= 12 and (len(words) <= 3 or capitalized >= len(words) / 2
                                                                                 or not any(word.islower() and len(word) > 3 for word in words[1:])):
            return line
        # a short plain phrase before a long paragraph ("Why we did this")
        if 2 <= len(words) <= 7 and line[:1].isupper() and next_words >= 20 and not re.search(r"\b(?:is|are|was|were|has|have|will|can)\b", line, re.I):
            return line
        return None

    @classmethod
    def _role_for(cls, name: str) -> str:
        cleaned = re.sub(r"^[^a-z]+", "", name.lower()).strip()
        if cleaned in ROLE_MAPPING:
            return ROLE_MAPPING[cleaned]
        matches = [(keyword, role) for keyword, role in ROLE_MAPPING.items()
                   if re.search(r"(?<!\w)" + re.escape(keyword) + r"(?!\w)", cleaned)]
        return max(matches, key=lambda match: len(match[0]))[1] if matches else "body"

    @classmethod
    def _detect_shaped_headings(cls, text: str) -> Dict[str, Any]:
        lines = [line.strip() for line in text.split("\n") if line.strip()]
        found: List[tuple] = []          # (first line, last line, name) of each heading
        index = 0
        while index < len(lines):
            line = lines[index]
            following = lines[index + 1] if index + 1 < len(lines) else ""
            name = cls._heading_text(line, following)
            end = index
            if name and cls._CHAPTER_RE.match(line) and not cls._CHAPTER_RE.match(line).group(2).strip() and following \
                    and len(following.split()) <= 12 and not following.endswith((".", ",", ";")) and not cls._NOT_HEADING_RE.search(following):
                name, end = following.strip(), index + 1     # "CHAPTER 1" then its title on the next line
            if name:
                found.append((index, end, name))
            index = end + 1
        # a line repeated many times is a running page header, not a heading
        repeats: Dict[str, int] = {}
        for _, _, name in found:
            repeats[name.lower()] = repeats.get(name.lower(), 0) + 1
        found = [item for item in found if repeats[item[2].lower()] < 4]

        sections_lines: Dict[str, List[str]] = {}
        roles: Dict[str, str] = {}
        display_names: Dict[str, str] = {}
        seen: Dict[str, int] = {}
        for position, (heading_start, end, name) in enumerate(found):
            body_end = found[position + 1][0] if position + 1 < len(found) else len(lines)   # up to the next heading's first line
            key = re.sub(r"^[^A-Za-z]+", "", name).strip().lower() or name.lower()
            seen[key] = seen.get(key, 0) + 1
            if seen[key] > 1:
                key = f"{key} ({seen[key]})"
            sections_lines[key] = lines[end + 1:body_end]
            display_names[key] = "\n".join(lines[heading_start:end + 1])
            roles[key] = cls._role_for(key)
            if roles[key] == "references":
                sections_lines[key] = lines[end + 1:]        # everything after the reference list stays with it
                break
        sections = {key: "\n".join(body).strip() for key, body in sections_lines.items()}
        return {"sections": sections, "confidence": 0.5, "detected_headings": [item[2] for item in found],
                "section_roles": roles, "section_order": list(sections), "display_names": display_names}

    @classmethod
    def _split_into_parts(cls, text: str) -> Dict[str, Any]:
        """No headings at all: equal parts in reading order, the first as the introduction and the last as the conclusion."""
        lines = [line.strip() for line in text.split("\n") if line.strip()]
        references: List[str] = []
        for position in range(len(lines) - 1, -1, -1):
            if re.fullmatch(r"(?i)(?:references|bibliography|works cited|literature cited)", lines[position]) \
                    and position >= len(lines) * 0.5:
                references = lines[position + 1:]
                lines = lines[:position]
                break
        pieces: List[str] = []
        for line in lines:
            if len(line.split()) > 400:                      # one giant paragraph: cut it at sentence ends
                sentences = re.split(r"(?<=[.!?])\s+", line)
                chunk: List[str] = []
                for sentence in sentences:
                    chunk.append(sentence)
                    if len(" ".join(chunk).split()) >= 150:
                        pieces.append(" ".join(chunk))
                        chunk = []
                if chunk:
                    pieces.append(" ".join(chunk))
            else:
                pieces.append(line)
        total = sum(len(piece.split()) for piece in pieces)
        if total < cls.MIN_WORDS_TO_SPLIT:
            return {"sections": {}, "confidence": 0.0, "detected_headings": [], "section_roles": {}, "section_order": []}
        count = max(4, min(8, round(total / 1200)))
        target = total / count
        groups: List[List[str]] = [[]]
        used = 0
        for piece in pieces:
            if len(groups) < count and used >= target * len(groups) and groups[-1]:
                groups.append([])
            groups[-1].append(piece)
            used += len(piece.split())
        sections: Dict[str, str] = {}
        roles: Dict[str, str] = {}
        for number, group in enumerate(groups, 1):
            name = f"part {number} of {len(groups)}"
            sections[name] = "\n".join(group)
            roles[name] = "introduction" if number == 1 else "conclusion" if number == len(groups) else "body"
        if references:
            sections["references"] = "\n".join(references)
            roles["references"] = "references"
        return {"sections": sections, "confidence": 0.2, "detected_headings": [], "section_roles": roles, "section_order": list(sections)}

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
        if (len(line) >= 80 or line.rstrip().endswith(".")
                or re.match(r"^(?:[*•☐☑☒\-]\s*|\d+[.)]\s+)", line)
                or re.search(r"(?:	|\s{2,})\d{1,3}$", line)
                or "|" in line or line.rstrip().endswith(",") or line.count("(") != line.count(")")
                or re.match(r"(?i)^(?:figure|fig\.|table|chart)\s+\d+", line)):
            return None

        remainder = prefix_re.sub("", line).strip()
        if remainder.endswith(":") or ":" in remainder or len(remainder) < 3:
            return None
        cleaned = remainder.lower()
        # Remove any remaining leading non-alpha characters
        cleaned = re.sub(r"^[^a-z]+", "", cleaned).strip()

        if not cleaned:
            return None

        if cleaned in ROLE_MAPPING:
            return ROLE_MAPPING[cleaned]
        words = re.findall(r"[A-Za-z]+", remainder)
        if (len(words) < 2 or sum(word[0].isupper() for word in words) < 2
                or sum(word[0].isupper() for word in words) < len(words) / 2):
            return None
        matches = [(keyword, role) for keyword, role in ROLE_MAPPING.items()
                   if re.search(r"(?<!\w)" + re.escape(keyword) + r"(?!\w)", cleaned)]
        return max(matches, key=lambda match: len(match[0]))[1] if matches else None

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
            # Allow heading decorations, not prose such as "References the user".
            suffix = clean_line[len(clean_header):].strip()
            if clean_line == clean_header or (
                clean_line.startswith(clean_header)
                and (suffix == "s" or suffix.startswith(("(", "/", "cont'd", "cont’d", "summary")))
            ):
                return header
        # Keep known academic headings under their own names when a template
        # uses different wording; do not relabel them as unrelated template topics.
        if clean_line in ROLE_MAPPING and (
            len(clean_line.split()) > 1 or clean_line in ROLE_MAPPING.values()
        ):
            return line.strip()
        return None
