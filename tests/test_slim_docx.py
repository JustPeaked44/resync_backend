import io
import zipfile

import main


def _docx(picture_bytes: int) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_STORED) as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("word/document.xml", "<w:document>Hello thesis</w:document>")
        z.writestr("word/media/image1.png", b"\x00" * picture_bytes)
    return out.getvalue()


def test_pictures_are_replaced_and_text_is_kept():
    big = _docx(2_000_000)
    slim = main.slim_docx_bytes(big)
    assert len(slim) < 20_000
    with zipfile.ZipFile(io.BytesIO(slim)) as z:
        assert z.read("word/document.xml") == b"<w:document>Hello thesis</w:document>"
        assert "word/media/image1.png" in z.namelist()


def test_small_or_broken_files_come_back_unchanged():
    small = _docx(100)
    assert main.slim_docx_bytes(small) == small
    junk = b"PK" + b"x" * 2_000_000
    assert main.slim_docx_bytes(junk) == junk
