"""Explorer launcher: `venv\\Scripts\\pythonw.exe open_book.py <path>`.

Starts the server if it is not running (detached, logs to logs/server.log), registers the book
through POST /api/open and opens the browser on #book=<id>. The path itself never goes into the URL.
Errors go to logs/open_book.log and a message box: pythonw has no console to print to.
"""
from __future__ import annotations

import json
import logging
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
LOG_DIR = ROOT / "logs"
PYTHONW = ROOT / "venv" / "Scripts" / "pythonw.exe"
HOST = "127.0.0.1"
DEFAULT_PORT = 8765
PING_TIMEOUT_S = 1
OPEN_TIMEOUT_S = 10
SERVER_START_TIMEOUT_S = 15
POLL_S = 0.25
APP_TITLE = "RSVP читалка"

log = logging.getLogger("rsvp.open_book")
# System proxy settings must not catch requests to 127.0.0.1.
_http = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class LaunchError(Exception):
    """Shown to the user as is."""


def port_from_config() -> int:
    try:
        return int(json.loads(CONFIG_PATH.read_text(encoding="utf-8")).get("port", DEFAULT_PORT))
    except (OSError, ValueError, AttributeError):
        return DEFAULT_PORT


def _url(port: int, route: str) -> str:
    return f"http://{HOST}:{port}{route}"


def server_alive(port: int, timeout: float = PING_TIMEOUT_S) -> bool:
    """True only if our reader answers: another program on the port gives no JSON list."""
    try:
        with _http.open(_url(port, "/api/books"), timeout=timeout) as res:
            return res.status == 200 and isinstance(json.loads(res.read().decode("utf-8")), list)
    except (OSError, ValueError):  # URLError, refused, timeout, not JSON
        return False


def start_server() -> None:
    if not PYTHONW.is_file():
        raise LaunchError(f"Не знайдено {PYTHONW}. Створіть venv (див. README).")
    LOG_DIR.mkdir(exist_ok=True)
    subprocess.Popen(
        [str(PYTHONW), str(ROOT / "server.py"), "--log-file", str(LOG_DIR / "server.log")],
        cwd=str(ROOT),
        creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        close_fds=True,
    )
    log.info("server started")


def wait_for_server(port: int, timeout: float, alive: Callable[[int], bool] = server_alive,
                    clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep) -> bool:
    deadline = clock() + timeout
    while True:
        if alive(port):
            return True
        if clock() >= deadline:
            return False
        sleep(POLL_S)


def post_open(port: int, path: str) -> str:
    request = urllib.request.Request(
        _url(port, "/api/open"), data=json.dumps({"path": path}).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with _http.open(request, timeout=OPEN_TIMEOUT_S) as res:
            return json.loads(res.read().decode("utf-8"))["id"]
    except urllib.error.HTTPError as exc:
        try:
            message = json.loads(exc.read().decode("utf-8")).get("error", exc.reason)
        except (ValueError, AttributeError):
            message = exc.reason
        raise LaunchError(f"Читалка не відкрила файл: {message}\n{path}") from exc
    except (OSError, ValueError, KeyError) as exc:
        raise LaunchError(f"Сервер читалки не відповів: {exc}") from exc


def launch(raw_path: str | None, port: int, alive: Callable[[int], bool], start: Callable[[], None],
           wait: Callable[[int, float], bool], post: Callable[[int, str], str],
           browse: Callable[[str], object]) -> None:
    if not raw_path:
        raise LaunchError("Не передано шлях до книги.")
    if not alive(port):
        start()
        if not wait(port, SERVER_START_TIMEOUT_S):
            raise LaunchError(f"Сервер не запустився за {SERVER_START_TIMEOUT_S} с. Журнал: {LOG_DIR / 'server.log'}")
    book_id = post(port, raw_path)
    browse(_url(port, f"/#book={book_id}"))


def message_box(text: str) -> None:
    import ctypes

    MB_ICONERROR, MB_TOPMOST = 0x10, 0x40000
    ctypes.windll.user32.MessageBoxW(None, text, APP_TITLE, MB_ICONERROR | MB_TOPMOST)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv if argv is None else argv
    LOG_DIR.mkdir(exist_ok=True)
    logging.basicConfig(filename=str(LOG_DIR / "open_book.log"), encoding="utf-8", level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    raw_path = argv[1] if len(argv) > 1 else None
    log.info("open %s", raw_path)
    try:
        launch(raw_path, port_from_config(), server_alive, start_server, wait_for_server, post_open, webbrowser.open)
        return 0
    except Exception as exc:  # pythonw: the box and the log are the only outputs
        log.exception("launch failed")
        message_box(str(exc) if isinstance(exc, LaunchError) else f"{type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
