"""Which books the reader knows: the recent list (recent.json), path checks, the file dialog."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Callable

from extract import SUPPORTED_SUFFIXES

RECENT_LIMIT = 50
SAVE_RETRIES = 5
SAVE_RETRY_S = 0.1

log = logging.getLogger("rsvp.library")


def book_id(path: Path | str) -> str:
    # str(Path) on Windows uses backslashes: the same ids the old folder scan gave,
    # so reading progress kept in the browser survives.
    return hashlib.sha1(str(Path(path)).encode("utf-8")).hexdigest()[:16]


def book_title(path: Path) -> str:
    return path.stem.replace("_", " ")


def validate_book_path(raw) -> Path:
    """Absolute path to an existing book file in a supported format, else ValueError."""
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("шлях до книги не задано")
    path = Path(raw.strip())
    if not path.is_absolute():
        raise ValueError("потрібен абсолютний шлях")
    if not path.is_file():
        raise ValueError("файл не знайдено")
    if path.suffix.lower() not in SUPPORTED_SUFFIXES:
        raise ValueError(f"формат {path.suffix or '(без розширення)'} не підтримано")
    return Path(os.path.abspath(path))


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


class RecentBooks:
    """recent.json: [{path, title, format, size_mb, opened_at}], newest first, at most `limit`."""

    def __init__(self, store: Path, limit: int = RECENT_LIMIT, now: Callable[[], str] = _now):
        self.store = store
        self.limit = limit
        self.now = now
        self._lock = threading.Lock()

    def _read(self) -> list[dict]:
        try:
            data = json.loads(self.store.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except (OSError, ValueError):
            log.exception("recent list unreadable, starting empty: %s", self.store)
            return []
        if not isinstance(data, list):
            return []
        return [e for e in data if isinstance(e, dict) and isinstance(e.get("path"), str)]

    def _write(self, entries: list[dict]) -> None:
        tmp = self.store.with_suffix(".tmp")
        tmp.write_text(json.dumps(entries, ensure_ascii=False, indent=1), encoding="utf-8")
        for attempt in range(SAVE_RETRIES):  # Windows refuses to replace a file being read
            try:
                tmp.replace(self.store)
                return
            except PermissionError:
                if attempt == SAVE_RETRIES - 1:
                    raise
                time.sleep(SAVE_RETRY_S)

    def add(self, path: Path) -> dict:
        """Put the book on top (moving it if listed); returns the entry with its id."""
        entry = {
            "path": str(path),
            "title": book_title(path),
            "format": path.suffix.lower().lstrip("."),
            "size_mb": round(path.stat().st_size / 1_048_576, 1),
            "opened_at": self.now(),
        }
        with self._lock:
            rest = [e for e in self._read() if e["path"] != entry["path"]]
            self._write([entry, *rest][: self.limit])
        return {**entry, "id": book_id(path)}

    def remove(self, wanted_id: str) -> bool:
        """Drop the entry from the list; the book file itself is never touched."""
        with self._lock:
            entries = self._read()
            kept = [e for e in entries if book_id(e["path"]) != wanted_id]
            if len(kept) == len(entries):
                return False
            self._write(kept)
            return True

    def entries(self) -> list[dict]:
        with self._lock:
            entries = self._read()
        return [{**e, "id": book_id(e["path"]), "available": Path(e["path"]).is_file()} for e in entries]

    def paths(self) -> dict[str, Path]:
        with self._lock:
            return {book_id(e["path"]): Path(e["path"]) for e in self._read()}


# ---------- native file dialog ----------

def tk_pick_file(initialdir: Path) -> str | None:
    """Blocking Open dialog. Call from one thread only: every Tk call stays in it."""
    import tkinter
    from tkinter import filedialog

    patterns = " ".join(f"*{s}" for s in sorted(SUPPORTED_SUFFIXES))
    root = tkinter.Tk()
    try:
        root.withdraw()
        root.attributes("-topmost", True)  # above the browser that asked for it
        chosen = filedialog.askopenfilename(
            parent=root,
            title="Відкрити книгу",
            initialdir=str(initialdir),
            filetypes=[("Книги", patterns), ("Усі файли", "*.*")],
        )
    finally:
        root.destroy()
    return chosen or None


class Picker:
    """One dialog at a time, each in its own thread; `dialog` is replaceable in tests."""

    def __init__(self, dialog: Callable[[Path], str | None] = tk_pick_file):
        self.dialog = dialog
        self._busy = threading.Lock()

    def pick(self, initialdir: Path) -> tuple[bool, str | None]:
        """-> (started, chosen path or None if cancelled); started=False if a dialog is open."""
        if not self._busy.acquire(blocking=False):
            return False, None
        try:
            result: dict = {}

            def run():
                try:
                    result["path"] = self.dialog(initialdir)
                except Exception as exc:  # reported to the caller, trace in the log
                    log.exception("file dialog failed")
                    result["error"] = exc

            thread = threading.Thread(target=run, name="file-dialog", daemon=True)
            thread.start()
            thread.join()
            if "error" in result:
                raise RuntimeError(f"діалог вибору файлу не відкрився: {result['error']}")
            return True, result.get("path")
        finally:
            self._busy.release()
