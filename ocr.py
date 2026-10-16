"""OCR for scanned books without a text layer: page image -> Tesseract -> text.

DjVu pages are rendered by `ddjvu` (DjVuLibre), PDF/CBZ/XPS pages by PyMuPDF.
Recognised pages are cached one file per page, so an interrupted run resumes.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable

import pymupdf

from extract import DJVU_SUFFIXES, PYMUPDF_LOCK, PYMUPDF_SUFFIXES

OCR_SUFFIXES = DJVU_SUFFIXES | PYMUPDF_SUFFIXES
OCR_DPI = 300
OCR_LANGS = "rus+ukr+eng"
TESSERACT_FALLBACK = Path("C:/Program Files/Tesseract-OCR/tesseract.exe")
PAGE_TIMEOUT_S = 180
MAX_WORKERS = 16

log = logging.getLogger("rsvp.ocr")


def _read_page(path: Path) -> str:
    # Older cache files hold "\r\r\n" per line (Tesseract's \r\n written in text mode);
    # dropping every \r keeps one line break per line, so blank lines still mark paragraphs.
    return path.read_bytes().decode("utf-8", errors="replace").replace("\r", "")


def default_workers() -> int:
    return max(1, min(MAX_WORKERS, (os.cpu_count() or 2) // 2))


def ocr_pages(
    total: int,
    render: Callable[[int], bytes],
    recognize: Callable[[bytes], str],
    cache_dir: Path,
    workers: int = 1,
    on_progress: Callable[[int, int], None] | None = None,
    failures: list[int] | None = None,
) -> list[str]:
    """OCR pages 0..total-1 in parallel; `cache_dir/NNNN.txt` holds page NNNN (0-based).

    A failed page becomes "" and its index goes to `failures`; if every page
    to do failed, the tool itself is broken and RuntimeError is raised.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    pages: list[str] = [""] * total
    todo: list[int] = []
    for i in range(total):
        cached = cache_dir / f"{i:04d}.txt"
        if cached.exists():
            pages[i] = _read_page(cached)
        else:
            todo.append(i)

    done = total - len(todo)
    if on_progress:
        on_progress(done, total)

    def job(i: int) -> str | None:
        try:
            text = recognize(render(i))
        except Exception:  # one bad page must not lose the whole book
            log.exception("OCR failed on page %d", i + 1)
            return None
        tmp = cache_dir / f"{i:04d}.tmp"
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(cache_dir / f"{i:04d}.txt")
        return text

    failed: list[int] = []
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {pool.submit(job, i): i for i in todo}
        for future in as_completed(futures):
            i, text = futures[future], future.result()
            if text is None:
                failed.append(i)
            pages[i] = text or ""
            done += 1
            if on_progress:
                on_progress(done, total)
    failed.sort()
    if failures is not None:
        failures.extend(failed)
    if todo and len(failed) == len(todo):
        raise RuntimeError(f"OCR: all {len(todo)} pages failed, see the server console")
    return pages


def cached_pages(cache_dir: Path, total: int) -> list[str] | None:
    """All `total` pages from the OCR cache, or None if any page is missing."""
    if total <= 0:
        return None
    pages: list[str] = []
    for i in range(total):
        cached = cache_dir / f"{i:04d}.txt"
        if not cached.is_file():
            return None
        pages.append(_read_page(cached))
    return pages


# ---------- concrete render / recognize ----------

def find_tesseract() -> str:
    exe = shutil.which("tesseract")
    if exe:
        return exe
    if TESSERACT_FALLBACK.exists():
        return str(TESSERACT_FALLBACK)
    raise RuntimeError("tesseract not found: install Tesseract-OCR 5 (UB Mannheim build)")


def tesseract_recognizer(tessdata_dir: Path, langs: str = OCR_LANGS) -> Callable[[bytes], str]:
    exe = find_tesseract()
    missing = [lang for lang in langs.split("+") if not (tessdata_dir / f"{lang}.traineddata").exists()]
    if missing:
        raise RuntimeError(f"no language models in {tessdata_dir}: {', '.join(missing)} (see README)")
    # One thread per process: parallelism comes from the worker pool.
    env = {**os.environ, "OMP_THREAD_LIMIT": "1"}
    cmd = [exe, "--tessdata-dir", str(tessdata_dir), "stdin", "stdout", "-l", langs]

    def recognize(image: bytes) -> str:
        result = subprocess.run(cmd, input=image, capture_output=True, env=env, timeout=PAGE_TIMEOUT_S, check=True)
        return result.stdout.decode("utf-8", errors="replace").replace("\r", "")

    return recognize


def _djvu_tool(name: str) -> str:
    exe = shutil.which(name)
    if not exe:
        raise RuntimeError(f"{name} not found: run `scoop install djvulibre`")
    return exe


def page_count(path: Path) -> int:
    if path.suffix.lower() in DJVU_SUFFIXES:
        result = subprocess.run(
            [_djvu_tool("djvused"), "-e", "n", str(path)], capture_output=True, timeout=60, check=True,
        )
        return int(result.stdout.strip())
    with PYMUPDF_LOCK, pymupdf.open(path) as doc:
        return doc.page_count


def page_renderer(path: Path) -> Callable[[int], bytes]:
    if path.suffix.lower() in DJVU_SUFFIXES:
        exe = _djvu_tool("ddjvu")

        def render_djvu(i: int) -> bytes:
            result = subprocess.run(
                [exe, "-format=pnm", f"-scale={OCR_DPI}", f"-page={i + 1}", str(path), "-"],
                capture_output=True, timeout=PAGE_TIMEOUT_S, check=True,
            )
            return result.stdout

        return render_djvu

    def render_pymupdf(i: int) -> bytes:
        with PYMUPDF_LOCK, pymupdf.open(path) as doc:
            pix = doc[i].get_pixmap(dpi=OCR_DPI, colorspace=pymupdf.csGRAY)
            return pix.tobytes("png")

    return render_pymupdf


def ocr_book(
    path: Path,
    cache_dir: Path,
    tessdata_dir: Path,
    workers: int | None = None,
    on_progress: Callable[[int, int], None] | None = None,
    failures: list[int] | None = None,
) -> list[str]:
    if path.suffix.lower() not in OCR_SUFFIXES:
        raise ValueError(f"OCR is not supported for {path.suffix}")
    recognize = tesseract_recognizer(tessdata_dir)
    return ocr_pages(
        page_count(path), page_renderer(path), recognize, cache_dir,
        workers=workers or default_workers(), on_progress=on_progress, failures=failures,
    )
