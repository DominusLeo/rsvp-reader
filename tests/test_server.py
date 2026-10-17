"""HTTP routes through a real server on a free port; recent list, caches and dialog are temp/fake."""
import http.client
import json
import logging
import os
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import server
from library import Picker, RecentBooks, book_id

WORDS = "Слово " * 40


class ServerTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.dialog_result = None
        self.picker = Picker(lambda initialdir: self.dialog(initialdir))
        patches = [
            mock.patch.object(server, "recent", RecentBooks(self.dir / "recent.json")),
            mock.patch.object(server, "picker", self.picker),
            mock.patch.object(server, "CACHE_DIR", self.dir / "cache"),
            mock.patch.object(server, "OCR_CACHE_DIR", self.dir / "cache" / "ocr"),
            mock.patch.dict(server._books, clear=True),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self._tmp.cleanup()

    def dialog(self, initialdir):
        return self.dialog_result

    def book(self, name="Тестова_книга.pdf"):
        path = self.dir / name
        path.write_bytes(b"%PDF-1.4 not really")
        return path

    def request(self, method, route, body=None, headers=None, raw=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        hdrs = {"Host": f"127.0.0.1:{self.port}"}
        hdrs.update(headers or {})
        payload = raw if raw is not None else (json.dumps(body).encode("utf-8") if body is not None else None)
        if payload is not None:
            hdrs.setdefault("Content-Type", "application/json")
        conn.request(method, route, body=payload, headers=hdrs)
        res = conn.getresponse()
        data = res.read()
        conn.close()
        return res.status, json.loads(data.decode("utf-8"))

    def listed_ids(self):
        status, books = self.request("GET", "/api/books")
        self.assertEqual(status, 200)
        return [b["id"] for b in books]

    # ---------- /api/open ----------
    def test_open_valid_path_returns_id_and_lists_book(self):
        path = self.book()
        status, data = self.request("POST", "/api/open", {"path": str(path)})
        self.assertEqual((status, data), (200, {"id": book_id(path)}))
        status, books = self.request("GET", "/api/books")
        self.assertEqual(len(books), 1)
        self.assertEqual(books[0]["id"], book_id(path))
        self.assertEqual(books[0]["title"], "Тестова книга")
        self.assertTrue(books[0]["available"])
        self.assertNotIn("path", books[0])  # the full path never leaves the server

    def test_relative_path_to_existing_file_is_rejected(self):
        existing = self.book()
        cwd = os.getcwd()
        os.chdir(self.dir)  # the file exists relative to the server's cwd, on any drive
        try:
            status, _ = self.request("POST", "/api/open", {"path": existing.name})
        finally:
            os.chdir(cwd)
        self.assertEqual(status, 400)
        self.assertEqual(self.listed_ids(), [])

    def test_open_rejects_bad_paths(self):
        folder = self.dir / "folder.pdf"
        folder.mkdir()
        text = self.dir / "notes.txt"
        text.write_text("x", encoding="utf-8")
        cases = {
            "missing": {"path": str(self.dir / "nope.pdf")},
            "directory": {"path": str(folder)},
            "suffix": {"path": str(text)},
            "not a string": {"path": 5},
            "no path": {},
        }
        for name, body in cases.items():
            with self.subTest(name):
                status, data = self.request("POST", "/api/open", body)
                self.assertEqual(status, 400)
                self.assertIn("error", data)
        self.assertEqual(self.listed_ids(), [])

    def test_open_rejects_bad_bodies(self):
        for name, raw in {"not json": b"{oops", "json list": b"[1]", "empty": b""}.items():
            with self.subTest(name):
                status, _ = self.request("POST", "/api/open", raw=raw)
                self.assertEqual(status, 400)

    def test_foreign_origin_and_host_are_refused(self):
        path = self.book()
        status, _ = self.request("POST", "/api/open", {"path": str(path)}, {"Origin": "http://evil.example"})
        self.assertEqual(status, 403)
        status, _ = self.request("POST", "/api/open", {"path": str(path)}, {"Host": "evil.example:80"})
        self.assertEqual(status, 403)
        self.assertEqual(self.listed_ids(), [])

    def test_own_origin_is_allowed(self):
        path = self.book()
        status, _ = self.request("POST", "/api/open", {"path": str(path)},
                                 {"Origin": f"http://localhost:{self.port}"})
        self.assertEqual(status, 200)

    # ---------- /api/recent/remove ----------
    def test_remove_drops_entry_but_keeps_file(self):
        path = self.book()
        _, data = self.request("POST", "/api/open", {"path": str(path)})
        status, _ = self.request("POST", "/api/recent/remove", {"id": data["id"]})
        self.assertEqual(status, 200)
        self.assertEqual(self.listed_ids(), [])
        self.assertTrue(path.is_file())

    def test_removed_book_is_no_longer_served(self):
        path = self.book()
        _, data = self.request("POST", "/api/open", {"path": str(path)})
        self.request("POST", "/api/recent/remove", {"id": data["id"]})
        status, _ = self.request("GET", f"/api/book/{data['id']}")
        self.assertEqual(status, 404)
        self.assertEqual(self.listed_ids(), [])  # and reading it did not put it back

    def test_remove_unknown_id_is_404(self):
        status, _ = self.request("POST", "/api/recent/remove", {"id": "0123456789abcdef"})
        self.assertEqual(status, 404)

    def test_missing_file_is_listed_unavailable_and_not_served(self):
        path = self.book()
        _, data = self.request("POST", "/api/open", {"path": str(path)})
        path.unlink()
        _, books = self.request("GET", "/api/books")
        self.assertFalse(books[0]["available"])
        status, _ = self.request("GET", f"/api/book/{data['id']}")
        self.assertEqual(status, 404)

    # ---------- /api/pick ----------
    def test_pick_returns_id_of_chosen_file(self):
        path = self.book()
        self.dialog_result = str(path)
        status, data = self.request("POST", "/api/pick")
        self.assertEqual((status, data), (200, {"id": book_id(path)}))
        self.assertEqual(self.listed_ids(), [book_id(path)])

    def test_pick_cancel(self):
        status, data = self.request("POST", "/api/pick")
        self.assertEqual((status, data), (200, {"cancelled": True}))

    def test_second_dialog_while_one_is_open_is_409(self):
        entered, release = threading.Event(), threading.Event()

        def slow_dialog(initialdir):
            entered.set()
            release.wait(10)
            return None

        self.picker.dialog = slow_dialog
        first: dict = {}
        worker = threading.Thread(target=lambda: first.update(r=self.request("POST", "/api/pick")))
        worker.start()
        self.assertTrue(entered.wait(5))
        status, _ = self.request("POST", "/api/pick")
        release.set()
        worker.join(10)
        self.assertEqual(status, 409)
        self.assertEqual(first["r"], (200, {"cancelled": True}))
        # the lock is released: the next dialog opens again
        self.picker.dialog = lambda initialdir: None
        self.assertEqual(self.request("POST", "/api/pick")[0], 200)

    def test_dialog_failure_is_500(self):
        def broken(initialdir):
            raise OSError("no display")

        self.picker.dialog = broken
        status, data = self.request("POST", "/api/pick")
        self.assertEqual(status, 500)
        self.assertIn("no display", data["error"])

    # ---------- /api/book: OCR cache rebuild ----------
    def test_scan_with_complete_ocr_cache_is_rebuilt_from_it(self):
        path = self.book("Скан.pdf")
        cache = server.OCR_CACHE_DIR / server._source_key(path)
        cache.mkdir(parents=True)
        for i in range(3):
            (cache / f"{i:04d}.txt").write_text(f"{WORDS}\n\nДругий абзац {i}.", encoding="utf-8")
        _, data = self.request("POST", "/api/open", {"path": str(path)})
        with mock.patch.object(server, "extract", return_value=(["", "", ""], [])), \
                mock.patch.object(server, "page_count", return_value=3):
            status, book = self.request("GET", f"/api/book/{data['id']}")
        self.assertEqual(status, 200)
        self.assertTrue(book["ocr"])
        self.assertFalse(book["no_text"])
        self.assertEqual(len(book["page_starts"]), 3)
        self.assertEqual(len(book["para_starts"]), 6)

    def test_scan_with_incomplete_ocr_cache_stays_no_text(self):
        path = self.book("Скан.pdf")
        cache = server.OCR_CACHE_DIR / server._source_key(path)
        cache.mkdir(parents=True)
        (cache / "0000.txt").write_text(WORDS, encoding="utf-8")
        _, data = self.request("POST", "/api/open", {"path": str(path)})
        with mock.patch.object(server, "extract", return_value=(["", "", ""], [])), \
                mock.patch.object(server, "page_count", return_value=3):
            status, book = self.request("GET", f"/api/book/{data['id']}")
        self.assertEqual(status, 200)
        self.assertTrue(book["no_text"])
        self.assertNotIn("ocr", book)


class BusyPortTest(unittest.TestCase):
    """pythonw has no console: a port already taken must end in the log file and exit code 1."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.log = Path(self._tmp.name) / "server.log"
        patch = mock.patch.object(server, "recent", RecentBooks(Path(self._tmp.name) / "recent.json"))
        patch.start()
        self.addCleanup(patch.stop)

    def tearDown(self):
        root = logging.getLogger()
        for handler in root.handlers[:]:  # main() configured the root logger with our temp file
            handler.close()
            root.removeHandler(handler)
        self._tmp.cleanup()

    def run_on_busy_port(self, reuse):
        occupant = socket.socket()
        if reuse:  # what the old server (socketserver default) sets on its socket
            occupant.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        occupant.bind(("127.0.0.1", 0))
        occupant.listen()
        port = occupant.getsockname()[1]
        try:
            with mock.patch.object(server.ThreadingHTTPServer, "serve_forever",
                                   side_effect=AssertionError("must not serve on a taken port")):
                rc = server.main(["--port", str(port), "--log-file", str(self.log)])
        finally:
            occupant.close()
        return rc, port

    def test_busy_port_is_logged_and_exits_1(self):
        rc, port = self.run_on_busy_port(reuse=False)
        self.assertEqual(rc, 1)
        self.assertIn(f"cannot listen on 127.0.0.1:{port}", self.log.read_text(encoding="utf-8"))

    def test_port_held_by_a_reuse_socket_is_busy_too(self):
        # Windows SO_REUSEADDR lets a second server bind the same port and steal its requests.
        rc, _ = self.run_on_busy_port(reuse=True)
        self.assertEqual(rc, 1)


if __name__ == "__main__":
    unittest.main()
