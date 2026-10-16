"""OCR orchestration with fake render/recognize: no Tesseract needed."""
import tempfile
import threading
import unittest
from pathlib import Path

from ocr import ocr_pages


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


if __name__ == "__main__":
    unittest.main()
