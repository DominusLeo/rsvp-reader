"""OCR orchestration with fake render/recognize: no Tesseract needed."""
import tempfile
import threading
import unittest
from pathlib import Path

from ocr import cached_pages, ocr_pages


class CachedPagesTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.cache = Path(self._tmp.name) / "ocr"

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, i, text):
        self.cache.mkdir(parents=True, exist_ok=True)
        (self.cache / f"{i:04d}.txt").write_text(text, encoding="utf-8")

    def test_returns_all_pages_in_order_when_complete(self):
        for i in (2, 0, 1):
            self.write(i, f"page {i}")
        self.assertEqual(cached_pages(self.cache, 3), ["page 0", "page 1", "page 2"])

    def test_missing_page_means_none(self):
        self.write(0, "a")
        self.write(2, "c")
        self.assertIsNone(cached_pages(self.cache, 3))

    def test_missing_dir_means_none(self):
        self.assertIsNone(cached_pages(self.cache, 3))

    def test_zero_pages_means_none(self):
        self.cache.mkdir(parents=True)
        self.assertIsNone(cached_pages(self.cache, 0))

    def test_crcrlf_from_old_cache_is_one_line_break(self):
        # Tesseract's \r\n went through write_text on Windows: every line ended "\r\r\n",
        # which universal newlines read back as a blank line after each line.
        self.cache.mkdir(parents=True)
        (self.cache / "0000.txt").write_bytes(b"line one\r\r\nline two\r\r\n\r\r\nnext para")
        self.assertEqual(cached_pages(self.cache, 1), ["line one\nline two\n\nnext para"])

    def test_ocr_pages_reads_cached_page_with_same_newline_fix(self):
        self.cache.mkdir(parents=True)
        (self.cache / "0000.txt").write_bytes(b"a\r\r\nb")
        self.assertEqual(ocr_pages(1, lambda i: b"", lambda img: "", self.cache), ["a\nb"])

    def test_unfinished_tmp_file_does_not_count(self):
        self.write(0, "a")
        (self.cache / "0001.tmp").write_text("half", encoding="utf-8")
        self.assertIsNone(cached_pages(self.cache, 2))


class OcrPagesTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.cache = Path(self._tmp.name) / "ocr"
        self.rendered = []
        self.lock = threading.Lock()

    def tearDown(self):
        self._tmp.cleanup()

    def render(self, i):
        with self.lock:
            self.rendered.append(i)
        return f"img{i}".encode()

    @staticmethod
    def recognize(image):
        return f"text of {image.decode()}"

    def test_returns_pages_in_order_with_parallel_workers(self):
        pages = ocr_pages(6, self.render, self.recognize, self.cache, workers=3)
        self.assertEqual(pages, [f"text of img{i}" for i in range(6)])

    def test_reports_progress_up_to_total(self):
        seen = []
        ocr_pages(4, self.render, self.recognize, self.cache, workers=2, on_progress=lambda d, t: seen.append((d, t)))
        self.assertEqual(max(seen), (4, 4))

    def test_resumes_from_cached_pages(self):
        self.cache.mkdir(parents=True)
        (self.cache / "0001.txt").write_text("cached page", encoding="utf-8")
        pages = ocr_pages(3, self.render, self.recognize, self.cache, workers=1)
        self.assertEqual(pages[1], "cached page")
        self.assertEqual(sorted(self.rendered), [0, 2])

    def test_failed_page_becomes_empty_not_fatal(self):
        def flaky(image):
            if image == b"img1":
                raise RuntimeError("tesseract crashed")
            return "ok"
        pages = ocr_pages(3, self.render, flaky, self.cache, workers=1)
        self.assertEqual(pages, ["ok", "", "ok"])

    def test_failed_pages_are_reported_and_not_cached(self):
        def flaky(image):
            if image == b"img1":
                raise RuntimeError("tesseract crashed")
            return "ok"
        failed = []
        ocr_pages(3, self.render, flaky, self.cache, workers=2, failures=failed)
        self.assertEqual(failed, [1])
        self.assertFalse((self.cache / "0001.txt").exists())

    def test_all_pages_failing_raises(self):
        def broken(image):
            raise RuntimeError("tesseract missing DLL")
        with self.assertRaisesRegex(RuntimeError, "all 3 pages failed"):
            ocr_pages(3, self.render, broken, self.cache, workers=2)


if __name__ == "__main__":
    unittest.main()
