"""Local RSVP reader server: `venv\\Scripts\\python server.py --open`.

Listens on 127.0.0.1 only. Serves static/ and a small JSON API:
  GET  /api/books       -> books found in config.json `book_dirs`
  GET  /api/book/<id>   -> {title, words, page_starts, no_text} (cached in cache/)
  POST /api/ocr/<id>    -> start background OCR of a scan (one book at a time)
  GET  /api/ocr/<id>    -> {state: idle|running|done|error, done, total, error}
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import threading
import webbrowser
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from extract import SUPPORTED_SUFFIXES, build_book, extract_pages
from ocr import OCR_SUFFIXES, ocr_book

ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "static"
CACHE_DIR = ROOT / "cache"
OCR_CACHE_DIR = CACHE_DIR / "ocr"
TESSDATA_DIR = ROOT / "tessdata"
CONFIG_PATH = ROOT / "config.json"
HOST = "127.0.0.1"
DEFAULT_PORT = 8765
# Bump when extraction/cleanup changes so stale caches are rebuilt.
EXTRACT_VERSION = 1

log = logging.getLogger("rsvp")

_books: dict[str, Path] = {}
_books_lock = threading.Lock()
_extract_lock = threading.Lock()
_ocr_jobs: dict[str, dict] = {}
_ocr_lock = threading.Lock()


def load_config() -> dict:
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8")) if CONFIG_PATH.exists() else {}
    config.setdefault("book_dirs", ["books"])
    config.setdefault("port", DEFAULT_PORT)
    return config


def scan_books(book_dirs: list[str]) -> dict[str, tuple[Path, str]]:
    """book id -> (path, name of the configured dir it was found in)."""
    found: dict[str, tuple[Path, str]] = {}
    for raw in book_dirs:
        base = (ROOT / raw).resolve() if not os.path.isabs(raw) else Path(raw)
        if not base.is_dir():
            log.warning("book dir not found: %s", base)
            continue
        for path in base.rglob("*"):
            if path.suffix.lower() in SUPPORTED_SUFFIXES and path.is_file():
                book_id = hashlib.sha1(str(path).encode("utf-8")).hexdigest()[:16]
                found.setdefault(book_id, (path, base.name))
    return found


def _source_key(path: Path) -> str:
    stat = path.stat()
    return hashlib.sha1(f"{path}|{stat.st_size}|{stat.st_mtime_ns}".encode("utf-8")).hexdigest()


def _cache_path(path: Path) -> Path:
    stat = path.stat()
    key = f"{path}|{stat.st_size}|{stat.st_mtime_ns}|{EXTRACT_VERSION}"
    return CACHE_DIR / f"{hashlib.sha1(key.encode('utf-8')).hexdigest()}.json"


def _save_book(path: Path, book: dict) -> None:
    cache = _cache_path(path)
    CACHE_DIR.mkdir(exist_ok=True)
    tmp = cache.with_suffix(".tmp")
    tmp.write_text(json.dumps(book, ensure_ascii=False), encoding="utf-8")
    tmp.replace(cache)


def load_book(path: Path) -> dict:
    cache = _cache_path(path)
    if cache.exists():
        return json.loads(cache.read_text(encoding="utf-8"))
    with _extract_lock:  # one heavy extraction at a time
        if cache.exists():
            return json.loads(cache.read_text(encoding="utf-8"))
        book = build_book(extract_pages(path))
        book["title"] = path.stem.replace("_", " ")
        _save_book(path, book)
        return book


def _run_ocr(book_id: str, path: Path) -> None:
    job = _ocr_jobs[book_id]

    def progress(done: int, total: int) -> None:
        with _ocr_lock:
            job.update(done=done, total=total)

    try:
        pages = ocr_book(path, OCR_CACHE_DIR / _source_key(path), TESSDATA_DIR, on_progress=progress)
        book = build_book(pages)
        book["title"] = path.stem.replace("_", " ")
        book["ocr"] = True
        _save_book(path, book)
        state = {"state": "done"}
    except Exception as exc:  # surfaced to the UI, full trace in the console
        log.exception("OCR failed: %s", path)
        state = {"state": "error", "error": f"{type(exc).__name__}: {exc}"}
    with _ocr_lock:
        job.update(state)
    log.info("OCR %s: %s", job["state"], path.name)


def start_ocr(book_id: str, path: Path) -> tuple[dict, HTTPStatus]:
    with _ocr_lock:
        running = [i for i, j in _ocr_jobs.items() if j["state"] == "running"]
        if running and running[0] != book_id:
            return {"error": "інша книга вже розпізнається, дочекайтесь завершення"}, HTTPStatus.CONFLICT
        if not running:
            _ocr_jobs[book_id] = {"state": "running", "done": 0, "total": 0}
            threading.Thread(target=_run_ocr, args=(book_id, path), daemon=True).start()
            log.info("OCR started: %s", path.name)
        return dict(_ocr_jobs[book_id]), HTTPStatus.ACCEPTED


def ocr_status(book_id: str) -> dict:
    with _ocr_lock:
        return dict(_ocr_jobs.get(book_id, {"state": "idle"}))


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(STATIC_DIR), **kwargs)

    def do_GET(self):
        route = self.path.split("?", 1)[0]
        if route == "/api/books":
            return self._list_books()
        if route.startswith("/api/book/"):
            return self._get_book(route.removeprefix("/api/book/"))
        if route.startswith("/api/ocr/"):
            return self._send_json(ocr_status(route.removeprefix("/api/ocr/")))
        return super().do_GET()

    def do_POST(self):
        # A page from another site must not start heavy jobs on this machine.
        origin = self.headers.get("Origin")
        if origin and origin != f"http://{self.headers.get('Host')}":
            return self._send_json({"error": "forbidden origin"}, HTTPStatus.FORBIDDEN)
        route = self.path.split("?", 1)[0]
        if not route.startswith("/api/ocr/"):
            return self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        book_id = route.removeprefix("/api/ocr/")
        path = self._book_path(book_id)
        if path is None:
            return self._send_json({"error": "unknown book id, reload the library"}, HTTPStatus.NOT_FOUND)
        if path.suffix.lower() not in OCR_SUFFIXES:
            return self._send_json({"error": f"OCR не підтримано для {path.suffix}"}, HTTPStatus.BAD_REQUEST)
        self._send_json(*start_ocr(book_id, path))

    @staticmethod
    def _book_path(book_id: str) -> Path | None:
        with _books_lock:
            return _books.get(book_id)

    def _list_books(self):
        found = scan_books(self.server.config["book_dirs"])
        with _books_lock:
            _books.clear()
            _books.update({book_id: path for book_id, (path, _) in found.items()})
        items = [
            {
                "id": book_id,
                "title": path.stem.replace("_", " "),
                "format": path.suffix.lower().lstrip("."),
                "size_mb": round(path.stat().st_size / 1_048_576, 1),
                "folder": path.parent.name,
                "root": root,
            }
            for book_id, (path, root) in found.items()
        ]
        items.sort(key=lambda b: b["title"].lower())
        self._send_json(items)

    def _get_book(self, book_id: str):
        path = self._book_path(book_id)
        if path is None:
            return self._send_json({"error": "unknown book id, reload the library"}, HTTPStatus.NOT_FOUND)
        try:
            book = load_book(path)
            book["can_ocr"] = path.suffix.lower() in OCR_SUFFIXES
            self._send_json(book)
        except Exception as exc:  # surfaced to the UI, full trace in the console
            log.exception("extraction failed: %s", path)
            self._send_json({"error": f"{type(exc).__name__}: {exc}"}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def _send_json(self, payload, status=HTTPStatus.OK):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        log.debug(fmt, *args)


def main():
    parser = argparse.ArgumentParser(description="Local RSVP speed reader")
    parser.add_argument("--open", action="store_true", help="open the browser after start")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    config = load_config()
    server = ThreadingHTTPServer((HOST, config["port"]), Handler)
    server.config = config
    url = f"http://{HOST}:{config['port']}/"
    log.info("RSVP reader on %s (book dirs: %s)", url, config["book_dirs"])
    if args.open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
