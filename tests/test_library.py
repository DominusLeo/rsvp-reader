"""Recent-books list and path validation: no server, temp files only."""
import hashlib
import json
import tempfile
import threading
import unittest
from pathlib import Path

from library import Picker, RecentBooks, book_id, tk_pick_file, validate_book_path


class RecentBooksTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.store = self.dir / "recent.json"
        self.clock = iter(f"2026-10-17T10:00:{s:02d}" for s in range(60))
        self.recent = RecentBooks(self.store, limit=3, now=lambda: next(self.clock))

    def tearDown(self):
        self._tmp.cleanup()

    def book(self, name, size=2_097_152):
        path = self.dir / name
        path.write_bytes(b"x" * size)
        return path

    def test_add_returns_id_and_stores_entry(self):
        path = self.book("Моя_книга.pdf")
        entry = self.recent.add(path)
        self.assertEqual(entry["id"], book_id(path))
        self.assertEqual(json.loads(self.store.read_text(encoding="utf-8")), [{
            "path": str(path), "title": "Моя книга", "format": "pdf", "size_mb": 2.0,
            "opened_at": "2026-10-17T10:00:00",
        }])

    def test_newest_first(self):
        a, b = self.book("a.pdf"), self.book("b.epub")
        self.recent.add(a)
        self.recent.add(b)
        self.assertEqual([e["title"] for e in self.recent.entries()], ["b", "a"])

    def test_re_adding_moves_to_top_without_duplicate(self):
        a, b = self.book("a.pdf"), self.book("b.pdf")
        self.recent.add(a)
        self.recent.add(b)
        self.recent.add(a)
        entries = self.recent.entries()
        self.assertEqual([e["title"] for e in entries], ["a", "b"])
        self.assertEqual(entries[0]["opened_at"], "2026-10-17T10:00:02")

    def test_limit_drops_the_oldest(self):
        paths = [self.book(f"{n}.pdf") for n in "abcd"]
        for p in paths:
            self.recent.add(p)
        self.assertEqual([e["title"] for e in self.recent.entries()], ["d", "c", "b"])

    def test_remove_by_id_keeps_the_book_file(self):
        a = self.book("a.pdf")
        self.recent.add(a)
        self.assertTrue(self.recent.remove(book_id(a)))
        self.assertEqual(self.recent.entries(), [])
        self.assertTrue(a.exists())
        self.assertFalse(self.recent.remove(book_id(a)))

    def test_missing_file_is_listed_as_unavailable(self):
        a = self.book("a.pdf")
        self.recent.add(a)
        a.unlink()
        entry = self.recent.entries()[0]
        self.assertFalse(entry["available"])
        self.assertEqual(entry["id"], book_id(a))

    def test_persists_across_instances(self):
        self.recent.add(self.book("a.pdf"))
        self.assertEqual([e["title"] for e in RecentBooks(self.store).entries()], ["a"])

    def test_corrupt_or_missing_store_reads_as_empty(self):
        self.assertEqual(self.recent.entries(), [])
        self.store.write_text("{not json", encoding="utf-8")
        self.assertEqual(RecentBooks(self.store).entries(), [])
        self.store.write_text('[{"title": "no path"}, 5]', encoding="utf-8")
        self.assertEqual(RecentBooks(self.store).entries(), [])

    def test_paths_maps_ids_to_paths(self):
        a = self.book("a.pdf")
        self.recent.add(a)
        self.assertEqual(self.recent.paths(), {book_id(a): a})

    def test_no_tmp_file_left_behind(self):
        self.recent.add(self.book("a.pdf"))
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["a.pdf", "recent.json"])


class PickerTest(unittest.TestCase):
    def test_returns_chosen_path_and_runs_dialog_in_its_own_thread(self):
        seen = {}

        def dialog(initialdir):
            seen["thread"] = threading.current_thread().name
            seen["dir"] = initialdir
            return "C:/b.pdf"

        self.assertEqual(Picker(dialog).pick(Path("C:/books")), (True, "C:/b.pdf"))
        self.assertEqual(seen, {"thread": "file-dialog", "dir": Path("C:/books")})

    def test_cancel_gives_none(self):
        self.assertEqual(Picker(lambda d: None).pick(Path(".")), (True, None))

    def test_second_dialog_while_one_is_open_is_refused(self):
        opened, release = threading.Event(), threading.Event()

        def slow(initialdir):
            opened.set()
            release.wait(5)
            return "C:/a.pdf"

        picker = Picker(slow)
        first = {}
        worker = threading.Thread(target=lambda: first.update(r=picker.pick(Path("."))))
        worker.start()
        self.assertTrue(opened.wait(5))
        self.assertEqual(picker.pick(Path(".")), (False, None))
        release.set()
        worker.join(5)
        self.assertEqual(first["r"], (True, "C:/a.pdf"))
        self.assertEqual(Picker(lambda d: "x").pick(Path(".")), (True, "x"))  # lock released

    def test_dialog_failure_raises_and_releases_the_lock(self):
        def broken(initialdir):
            raise OSError("no display")

        picker = Picker(broken)
        with self.assertRaisesRegex(RuntimeError, "no display"):
            picker.pick(Path("."))
        picker.dialog = lambda d: "ok"
        self.assertEqual(picker.pick(Path(".")), (True, "ok"))

    def test_tkinter_dialog_function_imports_in_a_worker_thread(self):
        # No window is opened: only the imports the real dialog needs are exercised.
        errors = []

        def load():
            try:
                import tkinter  # noqa: F401
                from tkinter import filedialog  # noqa: F401
            except Exception as exc:
                errors.append(exc)

        thread = threading.Thread(target=load)
        thread.start()
        thread.join(10)
        self.assertEqual(errors, [])
        self.assertTrue(callable(tk_pick_file))


class BookIdTest(unittest.TestCase):
    def test_id_is_sha1_of_the_windows_path_string(self):
        # Same ids as the old folder scan, so saved reading progress survives.
        expected = hashlib.sha1("C:\\Users\\x\\book.pdf".encode("utf-8")).hexdigest()[:16]
        self.assertEqual(book_id(Path("C:/Users/x/book.pdf")), expected)
        self.assertEqual(book_id("C:/Users/x/book.pdf"), expected)


class ValidateBookPathTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_accepts_existing_absolute_supported_file(self):
        path = self.dir / "k.DJVU"
        path.write_bytes(b"x")
        self.assertEqual(validate_book_path(str(path)), path)

    def test_rejects(self):
        (self.dir / "notes.txt").write_text("x", encoding="utf-8")
        (self.dir / "folder.pdf").mkdir()
        cases = {
            "relative": "books/a.pdf",
            "missing": str(self.dir / "nope.pdf"),
            "directory": str(self.dir / "folder.pdf"),
            "unsupported": str(self.dir / "notes.txt"),
            "empty": "",
        }
        for name, raw in cases.items():
            with self.subTest(name), self.assertRaises(ValueError):
                validate_book_path(raw)

    def test_rejects_non_string(self):
        for raw in (None, 5, ["a.pdf"]):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                validate_book_path(raw)


if __name__ == "__main__":
    unittest.main()
