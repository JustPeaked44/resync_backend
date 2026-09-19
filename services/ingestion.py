import logging
import re
import io
import httpx
from fastapi import HTTPException, status

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
                response = await client.get(export_url)
                
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
                
                raw_text = response.text
                
                # Catch Private Document Redirect (Google returns HTML login page)
                if "<html" in raw_text.lower() or "<!doctype html>" in raw_text.lower():
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
                import docx
                doc = docx.Document(io.BytesIO(file_bytes))
                lines = []
                for p in doc.paragraphs:
                    text = p.text.strip()
                    if text:
                        lines.append(text)
                # Also extract text from tables if any
                for table in doc.tables:
                    for row in table.rows:
                        row_cells = [cell.text.strip() for cell in row.cells if cell.text.strip()]
                        if row_cells:
                            # Avoid duplicate cells from merged rows/cols
                            unique_cells = []
                            for c in row_cells:
                                if not unique_cells or c != unique_cells[-1]:
                                    unique_cells.append(c)
                            if unique_cells:
                                lines.append(" | ".join(unique_cells))
                raw_text = "\n".join(lines)
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
