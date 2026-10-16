"""Book -> list of page texts -> flat word list with page, paragraph and chapter starts.

PDF / EPUB / FB2 / MOBI / XPS / CBZ go through PyMuPDF; DjVu goes through
`djvutxt` / `djvused` from DjVuLibre (scoop: `scoop install djvulibre`).
"""
from __future__ import annotations

import logging
import re
import shutil
import statistics
import subprocess
import threading
from pathlib import Path

import pymupdf

# PyMuPDF is not thread-safe even across separate documents: every call takes this lock.
PYMUPDF_LOCK = threading.Lock()
log = logging.getLogger("rsvp.extract")

PYMUPDF_SUFFIXES = {".pdf", ".epub", ".fb2", ".mobi", ".xps", ".cbz"}
DJVU_SUFFIXES = {".djvu", ".djv"}
SUPPORTED_SUFFIXES = PYMUPDF_SUFFIXES | DJVU_SUFFIXES

DJVUTXT_TIMEOUT_S = 300
DJVUSED_TIMEOUT_S = 60
# Fewer words per page than this on average = scan without a text layer.
MIN_WORDS_PER_PAGE = 20

# Pages without blank lines (djvutxt): a line shorter than this share of the
# page's median line length that ends with a terminal mark ends a paragraph.
SHORT_LINE_RATIO = 0.8
# PyMuPDF pages where most text blocks hold one line (some OCR'd PDFs put every
# line in its own block): blocks are not paragraphs there, use the line rule.
LINE_BLOCK_SHARE = 0.8
LINE_BLOCK_MIN_BLOCKS = 8
# Heuristic chapters (no TOC): a short paragraph that starts like a heading.
HEADING_MAX_WORDS = 10

# "розмовля¬\nти" (DjVu soft hyphen) and "кра-\nсивою" (hyphen + lowercase)
_SOFT_HYPHEN_BREAK = re.compile(r"[¬­]\s*\n\s*")
_HYPHEN_BREAK = re.compile(r"(\w)-\s*\n\s*(?=[a-zа-яіїєґ])")
# The whole line goes, newline included, so it leaves no blank line behind.
_PAGE_NUMBER_LINE = re.compile(r"^[ \t]*\d{1,4}[ \t]*(?:\n|$)", re.MULTILINE)
_BLANK_LINE = re.compile(r"\n[ \t\r\f\v]*\n\s*")
_WHITESPACE = re.compile(r"\s+")
_HAS_CORE_CHAR = re.compile(r"[^\W_]")
_TERMINAL = re.compile(r"[.!?…:»\"”]$")
_HEADING = re.compile(
    r"^(глава|розділ|раздел|частина|часть|chapter|part)\s+([\dIVXLCDM]+|[а-яіїєґa-z]+)\b", re.IGNORECASE,
)
_HEADING_KIND = {"глава": "chapter", "chapter": "chapter", "розділ": "section", "раздел": "section",
                 "частина": "part", "часть": "part", "part": "part"}
# Printed contents: 3+ same-kind headings with different numbers, each within this many words of the last.
CONTENTS_GAP_WORDS = 300
CONTENTS_MIN_HEADINGS = 3


_SENTENCE_END = re.compile(r"[.!?…]$")
_NUMBER = re.compile(r"[\dIVXLCDM]+", re.IGNORECASE)


def _is_heading(text: str) -> bool:
    """"Глава 3", "CHAPTER IV", "Розділ другий" - but not "Часть людей ушла."."""
    m = _HEADING.match(text)
    if not m or len(text.split()) > HEADING_MAX_WORDS:
        return False
    return bool(_NUMBER.fullmatch(m.group(2))) or not _SENTENCE_END.search(text)


def _join_hyphens(text: str) -> str:
    text = _SOFT_HYPHEN_BREAK.sub("", text)
    return _HYPHEN_BREAK.sub(r"\1", text)


def _split_by_short_lines(text: str) -> list[str]:
    lines = [line.strip() for line in text.split("\n")]
    lines = [line for line in lines if line]
    if not lines:
        return []
    short = SHORT_LINE_RATIO * statistics.median(len(line) for line in lines)
    paragraphs: list[str] = []
    current: list[str] = []
    for line in lines:
        if len(line) < short and _is_heading(line):
            # A short heading line is a paragraph of its own, or the heuristic never sees it;
            # a full-length line starting with "часть ..." is body text.
            if current:
                paragraphs.append(" ".join(current))
            paragraphs.append(line)
            current = []
            continue
        current.append(line)
        if len(line) < short and _TERMINAL.search(line):
            paragraphs.append(" ".join(current))
            current = []
    if current:
        paragraphs.append(" ".join(current))
    return paragraphs


def page_paragraphs(text: str) -> list[str]:
    """One page -> paragraphs with whitespace collapsed (hyphen breaks joined first)."""
    text = _PAGE_NUMBER_LINE.sub("", _join_hyphens(text)).strip()
    chunks = _BLANK_LINE.split(text) if _BLANK_LINE.search(text) else _split_by_short_lines(text)
    return [p for p in (_WHITESPACE.sub(" ", chunk).strip() for chunk in chunks) if p]


def clean_page(text: str) -> str:
    return " ".join(page_paragraphs(text))


def tokenize(text: str) -> list[str]:
    words: list[str] = []
    for token in text.split():
        if words and not _HAS_CORE_CHAR.search(token):
            words[-1] = f"{words[-1]} {token}"
        else:
            words.append(token)
    return words


def _toc_chapters(toc, page_starts: list[int], total: int) -> list[dict]:
    chapters = []
    for entry in toc or []:
        try:
            level, title, page = int(entry[0]), str(entry[1]), entry[2]
        except (TypeError, ValueError, IndexError):
            continue
        title = _WHITESPACE.sub(" ", title).strip()
        if not title or not isinstance(page, int) or not 1 <= page <= len(page_starts):
            continue
        word = max(0, min(page_starts[page - 1], total - 1))
        chapters.append({"title": title, "level": max(level, 1), "word": word})
    chapters.sort(key=lambda c: c["word"])  # stable: TOC order kept for equal words
    return chapters


def _heading_key(title: str) -> tuple[str, str]:
    m = _HEADING.match(title)
    return _HEADING_KIND[m.group(1).casefold()], m.group(2).casefold()


def _filter_headings(headings: list[dict]) -> list[dict]:
    """Drop printed-contents clusters, then keep only the first of each running header."""
    keys = [_heading_key(h["title"]) for h in headings]
    dropped: set[int] = set()
    for kind in {k for k, _ in keys}:
        idx = [i for i, k in enumerate(keys) if k[0] == kind]
        run: list[int] = []
        for i in idx + [None]:
            linked = (i is not None and run and keys[i] != keys[run[-1]]
                      and headings[i]["word"] - headings[run[-1]]["word"] < CONTENTS_GAP_WORDS)
            if linked:
                run.append(i)
                continue
            if len(run) >= CONTENTS_MIN_HEADINGS:
                dropped.update(run)
            run = [i] if i is not None else []
    seen: set[tuple[str, str]] = set()
    kept = []
    for i, heading in enumerate(headings):
        if i in dropped or keys[i] in seen:
            continue
        seen.add(keys[i])
        kept.append(heading)
    return kept


def build_book(pages: list[str], toc=None) -> dict:
    """`toc` = [[level, title, page (1-based)], ...] as PyMuPDF `get_toc()` returns."""
    words: list[str] = []
    page_starts: list[int] = []
    para_starts: list[int] = []
    headings: list[dict] = []
    previous_ended = True  # did the last paragraph seen so far end with a terminal mark?
    for page in pages:
        page_starts.append(len(words))
        for k, paragraph in enumerate(page_paragraphs(page)):
            tokens = tokenize(paragraph)
            if not tokens:
                continue
            is_heading = len(tokens) <= HEADING_MAX_WORDS and _is_heading(paragraph)
            # The first paragraph of a page continues the previous one unless that one ended.
            if k > 0 or previous_ended or is_heading:
                para_starts.append(len(words))
            if is_heading:
                headings.append({"title": paragraph, "level": 1, "word": len(words)})
            words.extend(tokens)
            previous_ended = bool(_TERMINAL.search(paragraph))
    no_text = len(words) < MIN_WORDS_PER_PAGE * max(len(pages), 1)
    chapters = _toc_chapters(toc, page_starts, len(words)) if words else []
    return {
        "words": words,
        "page_starts": page_starts,
        "para_starts": para_starts,
        "chapters": chapters or _filter_headings(headings),
        "no_text": no_text,
    }


# ---------- formats ----------

def _pymupdf_page_text(page) -> str:
    """Text blocks joined by blank lines, so each block becomes a paragraph."""
    blocks = [b[4].strip() for b in page.get_text("blocks") if b[6] == 0 and b[4].strip()]
    single = sum(1 for b in blocks if "\n" not in b)
    if len(blocks) >= LINE_BLOCK_MIN_BLOCKS and single >= LINE_BLOCK_SHARE * len(blocks):
        return "\n".join(blocks)
    return "\n\n".join(blocks)


def extract(path: Path) -> tuple[list[str], list]:
    """-> (page texts, toc as [[level, title, page 1-based], ...])."""
    suffix = path.suffix.lower()
    if suffix in DJVU_SUFFIXES:
        return _djvu_pages(path), djvu_outline(path)
    if suffix in PYMUPDF_SUFFIXES:
        with PYMUPDF_LOCK, pymupdf.open(path) as doc:
            return [_pymupdf_page_text(page) for page in doc], doc.get_toc(simple=True)
    raise ValueError(f"unsupported format: {suffix}")


def extract_pages(path: Path) -> list[str]:
    return extract(path)[0]


def extract_toc(path: Path) -> list:
    """TOC only (for OCR'd books, whose text comes from elsewhere)."""
    suffix = path.suffix.lower()
    if suffix in DJVU_SUFFIXES:
        return djvu_outline(path)
    with PYMUPDF_LOCK, pymupdf.open(path) as doc:
        return doc.get_toc(simple=True)


def _djvu_tool(name: str) -> str:
    exe = shutil.which(name)
    if not exe:
        raise RuntimeError(f"{name} not found: run `scoop install djvulibre`")
    return exe


def _djvu_pages(path: Path) -> list[str]:
    # Pages without a text layer emit no form feed, so DjVu page numbers are
    # approximate; word positions are exact.
    result = subprocess.run(
        [_djvu_tool("djvutxt"), str(path)], capture_output=True, timeout=DJVUTXT_TIMEOUT_S, check=True,
    )
    return result.stdout.decode("utf-8", errors="replace").split("\f")


def djvu_outline(path: Path) -> list:
    """The book's outline; a broken one costs the chapters, never the text."""
    try:
        result = subprocess.run(
            [_djvu_tool("djvused"), "-e", "print-outline", str(path)],
            capture_output=True, timeout=DJVUSED_TIMEOUT_S, check=True,
        )
    except (OSError, RuntimeError, subprocess.SubprocessError):
        log.exception("djvused print-outline failed: %s", path)
        return []
    return parse_djvu_outline(result.stdout.decode("ascii", errors="replace"))


# ---------- djvused s-expressions ----------

_ESCAPES = {"n": b"\n", "t": b"\t", "r": b"\r", "a": b"\a", "b": b"\b", "f": b"\f", "v": b"\v"}
_OCTAL = "01234567"


def _sexp_tokens(text: str):
    """Yield "(", ")", or a decoded string; bare atoms come as ("atom", value)."""
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch in "()":
            yield ch
            i += 1
        elif ch == '"':
            out = bytearray()
            i += 1
            while True:
                if i >= n:
                    raise ValueError("unterminated string")
                ch = text[i]
                if ch == '"':
                    i += 1
                    break
                if ch == "\\" and i + 1 < n:
                    nxt = text[i + 1]
                    if nxt in _OCTAL:  # djvused prints UTF-8 bytes as \NNN
                        j = i + 1
                        while j < n and j < i + 4 and text[j] in _OCTAL:
                            j += 1
                        out.append(int(text[i + 1:j], 8) & 0xFF)
                        i = j
                    else:
                        out += _ESCAPES.get(nxt, nxt.encode("utf-8"))
                        i += 2
                    continue
                out += ch.encode("utf-8")
                i += 1
            yield out.decode("utf-8", errors="replace")
        elif ch.isspace():
            i += 1
        else:
            j = i
            while j < n and not text[j].isspace() and text[j] not in '()"':
                j += 1
            yield ("atom", text[i:j])
            i = j


def _sexp_parse(text: str):
    stack: list[list] = [[]]
    for token in _sexp_tokens(text):
        if token == "(":
            stack.append([])
        elif token == ")":
            if len(stack) == 1:
                raise ValueError("unbalanced )")
            done = stack.pop()
            stack[-1].append(done)
        else:
            stack[-1].append(token)
    if len(stack) != 1:
        raise ValueError("unbalanced (")
    return stack[0]


def parse_djvu_outline(text: str) -> list:
    """`djvused -e print-outline` -> [[level, title, page 1-based], ...]; only "#N" targets."""
    try:
        forms = _sexp_parse(text)
    except ValueError:
        return []
    result: list = []

    def walk(entries, level):
        for entry in entries:
            if not isinstance(entry, list) or len(entry) < 2:
                continue
            title, target = entry[0], entry[1]
            if isinstance(title, str) and isinstance(target, str) and re.fullmatch(r"#\d+", target):
                result.append([level, title, int(target[1:])])
            walk(entry[2:], level + 1)

    for form in forms:
        if isinstance(form, list) and form and form[0] == ("atom", "bookmarks"):
            walk(form[1:], 1)
    return result
