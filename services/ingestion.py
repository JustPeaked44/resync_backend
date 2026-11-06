import logging
import re
import io
import zipfile
import httpx
from fastapi import HTTPException, status

MAX_UPLOAD_BYTES = 25 * 1024 * 1024
MAX_DOWNLOAD_BYTES = 120 * 1024 * 1024
MAX_DECOMPRESSED_BYTES = 200 * 1024 * 1024
MAX_ARCHIVE_MEMBER_BYTES = 100 * 1024 * 1024
READ_CHUNK_BYTES = 1024 * 1024


def bounded_read(stream, limit: int) -> bytearray:
    """Read at most limit bytes from a binary stream, raising on overflow."""
    data = bytearray()
    while True:
        chunk = stream.read(min(READ_CHUNK_BYTES, limit - len(data) + 1))
        if not chunk:
            return data
        if len(data) + len(chunk) > limit:
            raise ValueError("Stream exceeds size limit")
        data.extend(chunk)


async def bounded_download(response, limit: int = MAX_DOWNLOAD_BYTES) -> bytearray:
    """Consume an HTTP response without buffering more than the byte limit."""
    data = bytearray()
    async for chunk in response.aiter_bytes(READ_CHUNK_BYTES):
        if len(data) + len(chunk) > limit:
            raise HTTPException(status_code=413, detail="This Google Doc is too large to open (over 120 MB).")
        data.extend(chunk)
    return data


def validate_docx_archive(file_bytes: bytes) -> None:
    """Reject oversized archives before python-docx decompresses their members."""
    with zipfile.ZipFile(io.BytesIO(file_bytes)) as archive:
        members = archive.infolist()
        if (len(members) > 20_000
                or sum(member.file_size for member in members) > MAX_DECOMPRESSED_BYTES
                or any(member.file_size > MAX_ARCHIVE_MEMBER_BYTES for member in members)):
            raise HTTPException(status_code=413, detail="This document is too large to open. Please use a smaller file.")
        for member in members:
            with archive.open(member) as stream:
                while stream.read(READ_CHUNK_BYTES):
                    pass


def extract_docx_text(file_bytes) -> str:
    """Extract top-level paragraphs first, then tables, without loading pictures or the whole XML tree."""
    from lxml import etree
    from docx.oxml import parse_xml
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    groups = [[], []]
    counts = [0, 0]

    def append(group, text):
        if not text:
            return
        groups[group].append(text)
        counts[group] += len(text) + 1
        if counts[group] > 500_002:
            cleaned = DocumentIngestionService.strip_document_noise("\n".join(groups[group]))
            DocumentIngestionService.enforce_size_limits(cleaned)
            groups[group] = [cleaned] if cleaned else []
            counts[group] = len(cleaned)

    validate_docx_archive(file_bytes)
    with zipfile.ZipFile(io.BytesIO(file_bytes)) as archive:
        with archive.open("word/document.xml") as stream:
            context = etree.iterparse(stream, events=("start", "end"), resolve_entities=False, no_network=True)
            depth = 0
            for event, element in context:
                if event == "start":
                    depth += 1
                    continue
                if depth == 3 and element.tag in (ns + "p", ns + "tbl"):
                    node = parse_xml(etree.tostring(element))
                    if element.tag == ns + "p":
                        append(0, Paragraph(node, None).text.strip())
                    else:
                        for row in Table(node, None).rows:
                            cells = [cell.text.strip() for cell in row.cells if cell.text.strip()]
                            unique = []
                            for cell in cells:
                                if not unique or cell != unique[-1]:
                                    unique.append(cell)
                            append(1, " | ".join(unique))
                    element.clear()
                    while element.getprevious() is not None:
                        del element.getparent()[0]
                depth -= 1
    return "\n".join(groups[0] + groups[1])


def looks_like_html_document(text: str) -> bool:
    return bool(re.match(r"(?is)^\s*(?:<!doctype\s+html\b|<html\b)", text.lstrip("\ufeff")))

class DocumentIngestionService:
    """
    Handles fetching and cleaning plaintext from public Google Docs URLs.
    Excludes images, charts, and non-text noise.
    """
    
    @staticmethod
    def extract_google_doc_id(url: str) -> str:
        """Extracts the unique Document ID from a Google Docs link."""
        pattern = r"/document/d/([a-zA-Z0-9_-]+)"
        match = re.search(pattern, url)
        if not match:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid Google Docs URL format. Please provide a valid shareable link."
            )
        return match.group(1)

    @classmethod
    async def fetch_plaintext_from_gdoc(cls, doc_url: str) -> str:
        """
        Asynchronously fetches plaintext directly from Google Docs export URL.
        Includes 30s timeout, browser headers, and SSL resilience.
        """
        doc_id = cls.extract_google_doc_id(doc_url)
        export_url = f"https://docs.google.com/document/d/{doc_id}/export?format=txt"

        # Browser User-Agent prevents Google throttling
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }

        # 60-second timeout; TLS verification stays on (not bypassed).
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=60.0,
            headers=headers
        ) as client:
            try:
                async with client.stream("GET", export_url) as response:
                    if response.status_code == 404:
                        raise HTTPException(
                            status_code=status.HTTP_404_NOT_FOUND,
                            detail="Google Doc not found. Please verify the document URL exists."
                        )
                    elif response.status_code != 200:
                        raise HTTPException(
                            status_code=status.HTTP_403_FORBIDDEN,
                            detail="Unable to access document. Make sure sharing is set to 'Anyone with the link can view'."
                        )
                    raw_text = (await bounded_download(response)).decode(response.encoding or "utf-8", errors="replace")

                # Catch Private Document Redirect (Google returns HTML login page)
                if looks_like_html_document(raw_text):
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail="Document access restricted. Please change Google Doc permissions to 'Anyone with the link can view'."
                    )

                # Clean invisible UTF-8 Byte Order Mark (BOM)
                raw_text = raw_text.lstrip('\ufeff')

                if not raw_text.strip():
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="The submitted Google Doc appears to be empty."
                    )
                
                # Clean non-text noise
                cleaned_text = cls.strip_document_noise(raw_text)
                return cls.enforce_size_limits(cleaned_text)

            except HTTPException:
                # Explicitly re-raise 400, 403, and 404 status codes
                raise
            except Exception as exc:
                logging.getLogger("resync.ingestion").error(
                    "Network error fetching Google Doc %s: %s", doc_id, exc
                )
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail=f"Network error while fetching the Google Doc ({type(exc).__name__})."
                )

    @classmethod
    async def extract_plaintext_from_upload(cls, file_bytes: bytes, filename: str) -> str:
        """
        Extracts plaintext from an uploaded .pdf or .docx file.
        """
        filename_lower = filename.lower()
        
        try:
            if filename_lower.endswith(".docx"):
                raw_text = extract_docx_text(file_bytes)
            elif filename_lower.endswith(".pdf"):
                import pypdf
                pdf_reader = pypdf.PdfReader(io.BytesIO(file_bytes))
                pages_text = []
                for page in pdf_reader.pages:
                    text = page.extract_text()
                    if text:
                        pages_text.append(text)
                raw_text = "\n".join(pages_text)
            else:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Unsupported file format. Please upload a .docx or .pdf file."
                )
                
            if not raw_text.strip():
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="The uploaded document appears to be empty or contains no extractable text."
                )
                
            # Clean non-text noise
            cleaned_text = cls.strip_document_noise(raw_text)
            return cls.enforce_size_limits(cleaned_text)
        except HTTPException:
            raise
        except Exception as exc:
            logging.getLogger("resync.ingestion").error(
                "Error extracting text from uploaded file %s: %s", filename, exc
            )
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Unable to extract text from the uploaded file ({filename}). It may be corrupted or encrypted. ({type(exc).__name__})"
            )

    @staticmethod
    def strip_document_noise(text: str) -> str:
        """
        Removes figure captions, table labels, and ASCII formatting artifacts 
        so they do not distort sentence vector embeddings.
        """
        text = re.sub(r"(?i)^(figure|fig\.|table|chart)\s+\d+[\d\.]*.*$", "", text, flags=re.MULTILINE)
        text = re.sub(r"\r\n", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()
        
    @classmethod
    def enforce_size_limits(cls, text: str) -> str:
        """Enforces limits on document size to prevent out-of-memory errors on small instances."""
        char_count = len(text)
        
        if char_count > 200_000:
            logging.getLogger("resync.ingestion").warning(f"Large document ingested: {char_count} characters.")
            
        if char_count > 500_000:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=f"Document is too large to process ({char_count:,} characters). Max limit is 500,000 characters."
            )
            
        return text
