"""Explorer launcher: server start/wait logic on fakes, /api/open against a real server on a free port."""
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import open_book
import server
from library import RecentBooks, book_id


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t

    def sleep(self, s):
        self.t += s


class WaitForServerTest(unittest.TestCase):
    def test_returns_true_once_server_answers(self):
        answers = iter([False, False, True])
        clock = FakeClock()
        self.assertTrue(open_book.wait_for_server(8765, 15, alive=lambda port: next(answers),
                                                  clock=clock.now, sleep=clock.sleep))
        self.assertLess(clock.t, 15)

    def test_gives_up_after_timeout(self):
        clock = FakeClock()
        calls = []
        self.assertFalse(open_book.wait_for_server(8765, 15, alive=lambda port: calls.append(port) or False,
                                                   clock=clock.now, sleep=clock.sleep))
        self.assertGreaterEqual(clock.t, 15)
        self.assertLess(clock.t, 16)
        self.assertTrue(calls)


class LaunchTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.book = Path(self._tmp.name) / "Книга.pdf"
        self.book.write_bytes(b"%PDF")
        self.started = 0
        self.opened = []
        self.posted = []

    def tearDown(self):
        self._tmp.cleanup()

    def launch(self, alive_answers, wait_ok=True):
        answers = iter(alive_answers)

        def post(port, path):
            self.posted.append((port, path))
            return "0123456789abcdef"

        def start():
            self.started += 1

        return open_book.launch(
            str(self.book), port=8766, alive=lambda port: next(answers), start=start,
            wait=lambda port, timeout: wait_ok, post=post, browse=self.opened.append,
        )

    def test_running_server_is_reused(self):
        self.launch([True])
        self.assertEqual(self.started, 0)
        self.assertEqual(self.posted, [(8766, str(self.book))])
        self.assertEqual(self.opened, ["http://127.0.0.1:8766/#book=0123456789abcdef"])

    def test_server_is_started_when_down(self):
        self.launch([False])
        self.assertEqual(self.started, 1)
        self.assertEqual(len(self.opened), 1)

    def test_url_never_carries_the_path(self):
        self.launch([True])
        self.assertNotIn("Книга", self.opened[0])
        self.assertNotIn(self._tmp.name, self.opened[0])

    def test_server_that_never_comes_up_is_an_error(self):
        with self.assertRaises(open_book.LaunchError):
            self.launch([False], wait_ok=False)
        self.assertEqual(self.opened, [])

    def test_missing_argument_is_an_error(self):
        with self.assertRaises(open_book.LaunchError):
            open_book.launch(None, port=8766, alive=lambda p: True, start=lambda: None,
                             wait=lambda p, t: True, post=lambda p, b: "x", browse=self.opened.append)
        self.assertEqual(self.opened, [])


class StartServerTest(unittest.TestCase):
    def test_detached_pythonw_with_log_file_in_project_root(self):
        with mock.patch.object(open_book.subprocess, "Popen") as popen:
            open_book.start_server()
        args, kwargs = popen.call_args
        cmd = args[0]
        self.assertEqual(Path(cmd[0]), open_book.ROOT / "venv" / "Scripts" / "pythonw.exe")
        self.assertEqual(Path(cmd[1]), open_book.ROOT / "server.py")
        self.assertEqual(cmd[2:], ["--log-file", str(open_book.ROOT / "logs" / "server.log")])
        self.assertEqual(Path(kwargs["cwd"]), open_book.ROOT)
        flags = open_book.subprocess.DETACHED_PROCESS | open_book.subprocess.CREATE_NEW_PROCESS_GROUP
        self.assertEqual(kwargs["creationflags"] & flags, flags)


class RealServerTest(unittest.TestCase):
    """The launcher's own HTTP calls must pass the server's Host/Origin checks."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        for p in (mock.patch.object(server, "recent", RecentBooks(self.dir / "recent.json")),
                  mock.patch.dict(server._books, clear=True)):
            p.start()
            self.addCleanup(p.stop)
        self.httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self._tmp.cleanup()

    def test_alive_and_open(self):
        book = self.dir / "Книга.epub"
        book.write_bytes(b"x")
        self.assertTrue(open_book.server_alive(self.port))
        self.assertEqual(open_book.post_open(self.port, str(book)), book_id(book))

    def test_rejected_path_reports_server_message(self):
        with self.assertRaises(open_book.LaunchError) as ctx:
            open_book.post_open(self.port, str(self.dir / "нема.pdf"))
        self.assertIn("файл не знайдено", str(ctx.exception))

    def test_dead_port_is_not_alive(self):
        port = self.port
        self.httpd.shutdown()
        self.httpd.server_close()
        self.assertFalse(open_book.server_alive(port))
        # tearDown shuts a running server down: give it a fresh one
        self.httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()


if __name__ == "__main__":
    unittest.main()
