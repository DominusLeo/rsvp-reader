"""Book -> list of page texts -> flat word list for the RSVP reader.

PDF / EPUB / FB2 / MOBI / XPS / CBZ go through PyMuPDF; DjVu goes through
`djvutxt` from DjVuLibre (scoop: `scoop install djvulibre`).
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pymupdf

PYMUPDF_SUFFIXES = {".pdf", ".epub", ".fb2", ".mobi", ".xps", ".cbz"}
DJVU_SUFFIXES = {".djvu", ".djv"}
SUPPORTED_SUFFIXES = PYMUPDF_SUFFIXES | DJVU_SUFFIXES

DJVUTXT_TIMEOUT_S = 300
# Fewer words per page than this on average = scan without a text layer.
MIN_WORDS_PER_PAGE = 20

# "розмовля¬\nти" (DjVu soft hyphen) and "кра-\nсивою" (hyphen + lowercase)
_SOFT_HYPHEN_BREAK = re.compile(r"[¬­]\s*\n\s*")
_HYPHEN_BREAK = re.compile(r"(\w)-\s*\n\s*(?=[a-zа-яіїєґ])")
_PAGE_NUMBER_LINE = re.compile(r"^\s*\d{1,4}\s*$", re.MULTILINE)
_WHITESPACE = re.compile(r"\s+")
_HAS_CORE_CHAR = re.compile(r"[^\W_]")


def clean_page(text: str) -> str:
    text = _SOFT_HYPHEN_BREAK.sub("", text)
    text = _HYPHEN_BREAK.sub(r"\1", text)
    text = _PAGE_NUMBER_LINE.sub("", text)
    return _WHITESPACE.sub(" ", text).strip()


def tokenize(text: str) -> list[str]:
    words: list[str] = []
    for token in text.split():
        if words and not _HAS_CORE_CHAR.search(token):
            words[-1] = f"{words[-1]} {token}"
        else:
            words.append(token)
    return words


def build_book(pages: list[str]) -> dict:
    words: list[str] = []
    page_starts: list[int] = []
    for page in pages:
        page_starts.append(len(words))
        words.extend(tokenize(clean_page(page)))
    no_text = len(words) < MIN_WORDS_PER_PAGE * max(len(pages), 1)
    return {"words": words, "page_starts": page_starts, "no_text": no_text}


def extract_pages(path: Path) -> list[str]:
    suffix = path.suffix.lower()
    if suffix in DJVU_SUFFIXES:
        return _djvu_pages(path)
    if suffix in PYMUPDF_SUFFIXES:
        with pymupdf.open(path) as doc:
            return [page.get_text() for page in doc]
    raise ValueError(f"unsupported format: {suffix}")


def _djvu_pages(path: Path) -> list[str]:
    exe = shutil.which("djvutxt")
    if not exe:
        raise RuntimeError("djvutxt not found: run `scoop install djvulibre`")
    # Pages without a text layer emit no form feed, so DjVu page numbers are
    # approximate; word positions are exact.
    result = subprocess.run(
        [exe, str(path)], capture_output=True, timeout=DJVUTXT_TIMEOUT_S, check=True,
    )
    return result.stdout.decode("utf-8", errors="replace").split("\f")
