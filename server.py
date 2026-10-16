"""Local RSVP reader server: `venv\\Scripts\\python server.py --open`.

Listens on 127.0.0.1 only. Serves static/ and a small JSON API:
  GET /api/books        -> books found in config.json `book_dirs`
  GET /api/book/<id>    -> {title, words, page_starts, no_text} (cached in cache/)
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

ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "static"
CACHE_DIR = ROOT / "cache"
CONFIG_PATH = ROOT / "config.json"
HOST = "127.0.0.1"
DEFAULT_PORT = 8765
# Bump when extraction/cleanup changes so stale caches are rebuilt.
EXTRACT_VERSION = 1

log = logging.getLogger("rsvp")

_books: dict[str, Path] = {}
_books_lock = threading.Lock()
_extract_lock = threading.Lock()


def load_config() -> dict:
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8")) if CONFIG_PATH.exists() else {}
    config.setdefault("book_dirs", ["books"])
    config.setdefault("port", DEFAULT_PORT)
    return config


def scan_books(book_dirs: list[str]) -> dict[str, Path]:
    found: dict[str, Path] = {}
    for raw in book_dirs:
        base = (ROOT / raw).resolve() if not os.path.isabs(raw) else Path(raw)
        if not base.is_dir():
            log.warning("book dir not found: %s", base)
            continue
        for path in base.rglob("*"):
            if path.suffix.lower() in SUPPORTED_SUFFIXES and path.is_file():
                book_id = hashlib.sha1(str(path).encode("utf-8")).hexdigest()[:16]
                found[book_id] = path
    return found


def _cache_path(path: Path) -> Path:
    stat = path.stat()
    key = f"{path}|{stat.st_size}|{stat.st_mtime_ns}|{EXTRACT_VERSION}"
    return CACHE_DIR / f"{hashlib.sha1(key.encode('utf-8')).hexdigest()}.json"


def load_book(path: Path) -> dict:
    cache = _cache_path(path)
    if cache.exists():
        return json.loads(cache.read_text(encoding="utf-8"))
    with _extract_lock:  # one heavy extraction at a time
        if cache.exists():
            return json.loads(cache.read_text(encoding="utf-8"))
        book = build_book(extract_pages(path))
        book["title"] = path.stem.replace("_", " ")
        CACHE_DIR.mkdir(exist_ok=True)
        tmp = cache.with_suffix(".tmp")
        tmp.write_text(json.dumps(book, ensure_ascii=False), encoding="utf-8")
        tmp.replace(cache)
        return book


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(STATIC_DIR), **kwargs)

    def do_GET(self):
        route = self.path.split("?", 1)[0]
        if route == "/api/books":
            return self._list_books()
        if route.startswith("/api/book/"):
            return self._get_book(route.removeprefix("/api/book/"))
        return super().do_GET()

    def _list_books(self):
        found = scan_books(self.server.config["book_dirs"])
        with _books_lock:
            _books.clear()
            _books.update(found)
        items = [
            {
                "id": book_id,
                "title": path.stem.replace("_", " "),
                "format": path.suffix.lower().lstrip("."),
                "size_mb": round(path.stat().st_size / 1_048_576, 1),
                "folder": path.parent.name,
            }
            for book_id, path in found.items()
        ]
        items.sort(key=lambda b: b["title"].lower())
        self._send_json(items)

    def _get_book(self, book_id: str):
        with _books_lock:
            path = _books.get(book_id)
        if path is None:
            return self._send_json({"error": "unknown book id, reload the library"}, HTTPStatus.NOT_FOUND)
        try:
            self._send_json(load_book(path))
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
