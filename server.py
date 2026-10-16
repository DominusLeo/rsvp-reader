"""Local RSVP reader server: `venv\\Scripts\\python server.py --open`.

Listens on 127.0.0.1 only. Serves static/ and a small JSON API:
  GET  /api/books          -> recent books (recent.json), newest first
  GET  /api/book/<id>      -> {title, words, page_starts, para_starts, chapters, no_text} (cached in cache/)
  POST /api/pick           -> native Open dialog -> {id} | {cancelled: true}
  POST /api/open           -> {path} from the Explorer launcher -> {id}
  POST /api/recent/remove  -> {id}: drop from the list (the file stays)
  POST /api/ocr/<id>       -> start background OCR of a scan (one book at a time)
  GET  /api/ocr/<id>       -> {state: idle|running|done|error, done, total, error}
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sys
import threading
import time
import webbrowser
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# pythonw.exe (Explorer launcher) has no console: sys.stdout/err are None, and
# anything that prints (MuPDF warnings) would raise. Must run before the imports below.
for _name in ("stdout", "stderr"):
    if getattr(sys, _name) is None:
        setattr(sys, _name, open(os.devnull, "w", encoding="utf-8"))

from extract import build_book, extract, extract_toc  # noqa: E402
from library import Picker, RecentBooks, validate_book_path  # noqa: E402
from ocr import OCR_SUFFIXES, cached_pages, ocr_book, page_count  # noqa: E402

ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "static"
CACHE_DIR = ROOT / "cache"
OCR_CACHE_DIR = CACHE_DIR / "ocr"
TESSDATA_DIR = ROOT / "tessdata"
CONFIG_PATH = ROOT / "config.json"
RECENT_PATH = ROOT / "recent.json"
HOST = "127.0.0.1"
DEFAULT_PORT = 8765
# Bump when extraction/cleanup changes so stale caches are rebuilt.
EXTRACT_VERSION = 2
SAVE_RETRIES = 5
SAVE_RETRY_S = 0.1
MAX_BODY_BYTES = 64 * 1024

log = logging.getLogger("rsvp")

_books: dict[str, Path] = {}
_books_lock = threading.Lock()
_extract_lock = threading.Lock()
_ocr_jobs: dict[str, dict] = {}
_ocr_lock = threading.Lock()
recent = RecentBooks(RECENT_PATH)
picker = Picker()


def load_config() -> dict:
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8")) if CONFIG_PATH.exists() else {}
    config.setdefault("port", DEFAULT_PORT)
    return config


def remember_book(path: Path) -> str:
    """Add to the recent list and to the id -> path map; returns the book id."""
    entry = recent.add(path)
    with _books_lock:
        _books[entry["id"]] = path
    return entry["id"]


def load_known_books() -> None:
    with _books_lock:
        _books.update(recent.paths())


def pick_initial_dir() -> Path:
    for entry in recent.entries():
        folder = Path(entry["path"]).parent
        if folder.is_dir():
            return folder
        break  # only the last opened book counts
    desktop = Path.home() / "Desktop"
    return desktop if desktop.is_dir() else Path.home()


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
    # Windows refuses to replace a file another thread is reading right now.
    for attempt in range(SAVE_RETRIES):
        try:
            tmp.replace(cache)
            return
        except PermissionError:
            if attempt == SAVE_RETRIES - 1:
                raise
            time.sleep(SAVE_RETRY_S)


def _ocr_book_from_pages(path: Path, pages: list[str], toc: list, failed: int = 0) -> dict:
    book = build_book(pages, toc)
    book["title"] = path.stem.replace("_", " ")
    book["ocr"] = True
    book["ocr_failed_pages"] = failed
    return book


def _toc_or_empty(path: Path) -> list:
    try:
        return extract_toc(path)
    except Exception:  # a broken outline must not cost the text
        log.exception("TOC read failed: %s", path)
        return []


def _book_from_ocr_cache(path: Path, toc: list) -> dict | None:
    """A finished OCR survives an EXTRACT_VERSION bump: rebuild from page files, no Tesseract."""
    if path.suffix.lower() not in OCR_SUFFIXES:
        return None
    try:
        pages = cached_pages(OCR_CACHE_DIR / _source_key(path), page_count(path))
    except Exception:
        log.exception("OCR cache check failed: %s", path)
        return None
    return _ocr_book_from_pages(path, pages, toc) if pages is not None else None


def load_book(path: Path) -> dict:
    cache = _cache_path(path)
    if cache.exists():
        return json.loads(cache.read_text(encoding="utf-8"))
    with _extract_lock:  # one heavy extraction at a time
        if cache.exists():
            return json.loads(cache.read_text(encoding="utf-8"))
        pages, toc = extract(path)
        book = build_book(pages, toc)
        book["title"] = path.stem.replace("_", " ")
        if book["no_text"]:
            book = _book_from_ocr_cache(path, toc) or book
        _save_book(path, book)
        return book


def _run_ocr(book_id: str, path: Path) -> None:
    job = _ocr_jobs[book_id]

    failures: list[int] = []

    def progress(done: int, total: int) -> None:
        with _ocr_lock:
            job.update(done=done, total=total)

    try:
        pages = ocr_book(
            path, OCR_CACHE_DIR / _source_key(path), TESSDATA_DIR, on_progress=progress, failures=failures,
        )
        _save_book(path, _ocr_book_from_pages(path, pages, _toc_or_empty(path), len(failures)))
        state = {"state": "done", "failed": len(failures)}
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

    def _allowed_hosts(self) -> set[str]:
        port = self.server.server_address[1]
        return {f"127.0.0.1:{port}", f"localhost:{port}"}

    def _host_ok(self) -> bool:
        # DNS rebinding: a foreign name resolving to 127.0.0.1 arrives with its own Host.
        if self.headers.get("Host") in self._allowed_hosts():
            return True
        self._send_json({"error": "forbidden host"}, HTTPStatus.FORBIDDEN)
        return False

    def do_GET(self):
        if not self._host_ok():
            return
        route = self.path.split("?", 1)[0]
        if route == "/api/books":
            return self._list_books()
        if route.startswith("/api/book/"):
            return self._get_book(route.removeprefix("/api/book/"))
        if route.startswith("/api/ocr/"):
            return self._send_json(ocr_status(route.removeprefix("/api/ocr/")))
        return super().do_GET()

    def do_POST(self):
        if not self._host_ok():
            return
        # A page from another site must not start heavy jobs on this machine.
        origin = self.headers.get("Origin")
        if origin and origin not in {f"http://{host}" for host in self._allowed_hosts()}:
            return self._send_json({"error": "forbidden origin"}, HTTPStatus.FORBIDDEN)
        route = self.path.split("?", 1)[0]
        routes = {"/api/pick": self._pick, "/api/open": self._open, "/api/recent/remove": self._remove_recent}
        if route in routes:
            return routes[route]()
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

    def _read_json(self) -> dict | None:
        """Request body as a JSON object, or None after sending 400."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if not 0 < length <= MAX_BODY_BYTES:
            self._send_json({"error": "expected a JSON body"}, HTTPStatus.BAD_REQUEST)
            return None
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            data = None
        if not isinstance(data, dict):
            self._send_json({"error": "expected a JSON object"}, HTTPStatus.BAD_REQUEST)
            return None
        return data

    def _open(self):
        data = self._read_json()
        if data is None:
            return
        try:
            path = validate_book_path(data.get("path"))
        except ValueError as exc:
            return self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        self._send_json({"id": remember_book(path)})

    def _pick(self):
        try:
            started, chosen = picker.pick(pick_initial_dir())
        except RuntimeError as exc:
            return self._send_json({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)
        if not started:
            return self._send_json({"error": "діалог вибору файлу вже відкрито"}, HTTPStatus.CONFLICT)
        if chosen is None:
            return self._send_json({"cancelled": True})
        try:
            path = validate_book_path(chosen)
        except ValueError as exc:
            return self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        self._send_json({"id": remember_book(path)})

    def _remove_recent(self):
        data = self._read_json()
        if data is None:
            return
        if not isinstance(data.get("id"), str) or not recent.remove(data["id"]):
            return self._send_json({"error": "no such book in the list"}, HTTPStatus.NOT_FOUND)
        self._send_json({"removed": data["id"]})

    def _list_books(self):
        items = [
            {
                "id": e["id"], "title": e.get("title", ""), "format": e.get("format", ""),
                "size_mb": e.get("size_mb", 0), "folder": Path(e["path"]).parent.name,
                "opened_at": e.get("opened_at", ""), "available": e["available"],
            }
            for e in recent.entries()
        ]
        self._send_json(items)

    def _get_book(self, book_id: str):
        path = self._book_path(book_id)
        if path is None:
            return self._send_json({"error": "unknown book id, reload the library"}, HTTPStatus.NOT_FOUND)
        if not path.is_file():
            return self._send_json({"error": f"файл недоступний: {path}"}, HTTPStatus.NOT_FOUND)
        try:
            book = load_book(path)
            book["can_ocr"] = path.suffix.lower() in OCR_SUFFIXES
        except Exception as exc:  # surfaced to the UI, full trace in the log
            log.exception("extraction failed: %s", path)
            return self._send_json({"error": f"{type(exc).__name__}: {exc}"}, HTTPStatus.INTERNAL_SERVER_ERROR)
        remember_book(path)  # reading it again moves it to the top of "Нещодавні"
        self._send_json(book)

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
    parser.add_argument("--port", type=int, help="override `port` from config.json")
    parser.add_argument("--log-file", type=Path, help="log here instead of the console")
    args = parser.parse_args()
    log_kwargs = {}
    if args.log_file:
        args.log_file.parent.mkdir(parents=True, exist_ok=True)
        log_kwargs = {"filename": str(args.log_file), "encoding": "utf-8"}
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", **log_kwargs)

    config = load_config()
    port = args.port or config["port"]
    load_known_books()
    server = ThreadingHTTPServer((HOST, port), Handler)
    server.config = config
    url = f"http://{HOST}:{port}/"
    log.info("RSVP reader on %s (recent books: %d)", url, len(_books))
    if args.open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
