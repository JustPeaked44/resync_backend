"""Derive client-facing citation labels from saved citation fields."""

import re


_JS_SPACE = "\\s\\u00a0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000\\ufeff"
_TRIM = " \t\n\r\v\f\u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"
_NO_LINK = re.compile(rf"^no[{_JS_SPACE}_-]*link$", re.IGNORECASE | re.ASCII)
_LINK = re.compile(rf"(?:https?://|www\.|\b[\w-]+\.[a-z]{{2,}}(?:/|\b)|\b10\.\d{{4,9}}/|\bdoi[{_JS_SPACE}]*:)", re.IGNORECASE | re.ASCII)


def citation_display_status(citation: dict) -> str:
    if not isinstance(citation, dict):
        return "no_link"
    status = (citation.get("citation_status") or "").strip(_TRIM)
    no_link = bool(_NO_LINK.fullmatch(status)) if status else (
        not (citation.get("citation_primary_link") or "").strip(_TRIM)
        and not _LINK.search(citation.get("citation_raw_reference_text") or "")
    )
    if no_link:
        return "no_link"
    accessible = citation.get("citation_is_accessible")
    if accessible is True:
        return "accessible"
    if accessible is False:
        return "inaccessible"
    return "accessible" if status in {"verified_doi", "verified_url", "verified_metadata", "metadata_mismatch", "accessible"} else "inaccessible"


def add_citation_display_status(result):
    for citation in result.get("citations") or []:
        if isinstance(citation, dict):
            citation["display_status"] = citation_display_status(citation)
    return result
