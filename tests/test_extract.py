"""Text extraction and cleanup: run with `venv\\Scripts\\python -m unittest discover -s tests`."""
import tempfile
import unittest
from pathlib import Path

import pymupdf

from extract import build_book, clean_page, extract_pages, tokenize


class CleanPageTest(unittest.TestCase):
    def test_joins_djvu_soft_hyphen(self):
        self.assertEqual(clean_page("мені  розмовля¬\nти.  Вона"), "мені розмовляти. Вона")

    def test_joins_ascii_hyphen_at_line_end(self):
        self.assertEqual(clean_page("була кра-\nсивою"), "була красивою")

    def test_keeps_hyphen_inside_line(self):
        self.assertEqual(clean_page("поїхали в Нью-Йорк"), "поїхали в Нью-Йорк")

    def test_keeps_dash_before_capital_on_next_line(self):
        # "-\n" + uppercase is a real dash/list, not a word break
        self.assertEqual(clean_page("кінець -\nПочаток"), "кінець - Початок")

    def test_drops_lines_that_are_only_page_numbers(self):
        self.assertEqual(clean_page("текст сторінки\n  12  \nдалі"), "текст сторінки далі")


class TokenizeTest(unittest.TestCase):
    def test_splits_on_whitespace(self):
        self.assertEqual(tokenize("раз  два\nтри"), ["раз", "два", "три"])

    def test_attaches_standalone_punctuation_to_previous_word(self):
        self.assertEqual(tokenize("сказала вона — і пішла"), ["сказала", "вона —", "і", "пішла"])

    def test_leading_standalone_punctuation_is_kept(self):
        self.assertEqual(tokenize("— Ходімо"), ["—", "Ходімо"])


class BuildBookTest(unittest.TestCase):
    def test_page_starts_index_into_words(self):
        book = build_book(["один два", "", "три чотири п'ять"])
        self.assertEqual(book["words"], ["один", "два", "три", "чотири", "п'ять"])
        self.assertEqual(book["page_starts"], [0, 2, 2])

    def test_flags_scan_without_text_layer(self):
        self.assertTrue(build_book(["", "", "5"])["no_text"])
        self.assertFalse(build_book(["слово " * 50, "слово " * 50])["no_text"])


class ExtractPdfTest(unittest.TestCase):
    def test_extracts_one_string_per_pdf_page(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.pdf"
            doc = pymupdf.open()
            for text in ("first page", "second page"):
                doc.new_page().insert_text((72, 72), text)
            doc.save(path)
            doc.close()
            pages = extract_pages(path)
        self.assertEqual(len(pages), 2)
        self.assertIn("second page", pages[1])


if __name__ == "__main__":
    unittest.main()
