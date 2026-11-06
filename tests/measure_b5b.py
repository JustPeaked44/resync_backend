"""Fake-only memory probes; run with repository root as cwd and arguments: input-dir scenario."""
import asyncio
import io
import os
import struct
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, os.getcwd())


def generate(root):
    from docx import Document
    from docx.shared import Inches
    root.mkdir(parents=True, exist_ok=True)
    width, height = 2800, 2995
    pixels = os.urandom(width * height * 3)
    header = b"BM" + struct.pack("<IHHI", len(pixels) + 54, 0, 0, 54)
    header += struct.pack("<IiiHHIIiiII", 40, width, height, 1, 24, 0, len(pixels), 2835, 2835, 0, 0)
    doc = Document()
    doc.add_heading("Research paper", 1)
    doc.add_paragraph("Introduction and methods.")
    doc.add_picture(io.BytesIO(header + pixels), width=Inches(2))
    doc.save(root / "upload.docx")
    del doc, pixels
    with zipfile.ZipFile(root / "download.docx", "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("word/document.xml", b"<document/>")
        with archive.open("word/media/picture.bin", "w") as out:
            for _ in range(60):
                out.write(os.urandom(1024 * 1024))
    print("inputs", (root / "upload.docx").stat().st_size, (root / "download.docx").stat().st_size)


async def measure(root, scenario):
    import psutil
    import main
    from starlette.datastructures import UploadFile
    async def provision(req, **kwargs):
        return str(id(req)), "manuscript", 1
    main._provision_analysis_run = provision
    async def make_upload():
        return UploadFile(file=open(root / "upload.docx", "rb"), filename="paper.docx")
    if scenario == "outline":
        upload = await make_upload()
        await main.manuscript_outline(file=upload, doc_url=None, authenticated_user_id="u")
        await upload.close()
    elif scenario in ("start", "queue"):
        await main.SCAN_SLOT.acquire()
        count = 1 if scenario == "start" else 9
        accepted, rejected = 0, 0
        for i in range(count):
            if scenario == "queue":
                # Keep exactly 10 MiB per upload; the route does not parse before queueing.
                upload = UploadFile(file=io.BytesIO(b"x" * (10 * 1024 * 1024)), filename="paper.docx")
            else:
                upload = await make_upload()
            try:
                await main.upload_and_start_scan(file=upload, user_id=str(i), manuscript_id=None, manuscript_title=None,
                    template_toc=None, style_reference_url=None, authenticated_user_id=str(i))
                accepted += 1
            except main.HTTPException as exc:
                if exc.status_code != 429:
                    raise
                rejected += 1
            finally:
                await upload.close()
        print("accepted", accepted, "rejected", rejected, "queued", len(main._scan_queue))
        print("peak_wset", psutil.Process().memory_info().peak_wset)
        for task in main._scan_workers:
            task.cancel()
        await asyncio.gather(*main._scan_workers, return_exceptions=True)
        main.SCAN_SLOT.release()
        return
    elif scenario == "download":
        import httpx
        class Response:
            status_code = 200
            headers = {"content-type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"}
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            async def aiter_bytes(self, size):
                with open(root / "download.docx", "rb") as file:
                    while chunk := file.read(size):
                        yield chunk
        class Client:
            def __init__(self, *args, **kwargs): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            def stream(self, *args, **kwargs): return Response()
        httpx.AsyncClient = Client
        data, _ = await main.download_google_docx("fake")
        print("slim_bytes", len(data))
    print("peak_wset", psutil.Process().memory_info().peak_wset)


if __name__ == "__main__":
    root = Path(sys.argv[1])
    if sys.argv[2] == "generate":
        generate(root)
    else:
        asyncio.run(measure(root, sys.argv[2]))
