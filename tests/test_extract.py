"""Text extraction and cleanup: run with `venv\\Scripts\\python -m unittest discover -s tests`."""
import tempfile
import unittest
from pathlib import Path

import pymupdf

from extract import (
    build_book, clean_page, extract, extract_pages, page_paragraphs, parse_djvu_outline, tokenize,
)

# Real `djvused -e print-outline` output (DjVuLibre 3.5.29) for an outline with a
# Cyrillic title, an escaped quote and backslash, a nested entry and a URL target.
DJVU_OUTLINE = (
    b'(bookmarks\r\n ("\\320\\223\\320\\273\\320\\260\\320\\262\\320\\260 1. \\320\\222\\321\\201\\321\\202'
    b'\\321\\203\\320\\277 \\"\\321\\206\\320\\270\\321\\202\\320\\260\\321\\202\\320\\260\\" back\\\\slash"\r\n'
    b'  "#3"\r\n  ("1.1 \\320\\237\\321\\226\\320\\264\\321\\200\\320\\276\\320\\267\\320\\264\\321\\226\\320\\273"'
    b'\r\n   "#5" ) )\r\n ("Chapter 2"\r\n  "#10" )\r\n ("\\320\\227\\320\\276\\320\\262\\320\\275\\321\\226'
    b'\\321\\210\\320\\275\\321\\224 \\320\\277\\320\\276\\321\\201\\320\\270\\320\\273\\320\\260\\320\\275'
    b'\\320\\275\\321\\217"\r\n  "http://example.com" ) )\r\n'
).decode("ascii")


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


LONG = "довгий рядок тексту без жодного кінця речення тут"  # 49 chars, no terminal mark


class PageParagraphsTest(unittest.TestCase):
    def test_blank_line_separates_paragraphs(self):
        self.assertEqual(page_paragraphs("Перший абзац.\nПродовження\n\n  \nДругий абзац."),
                         ["Перший абзац. Продовження", "Другий абзац."])

    def test_leading_and_trailing_blank_lines_are_not_boundaries(self):
        self.assertEqual(page_paragraphs("\n\nрядок один\nрядок два\n\n"), ["рядок один рядок два"])

    def test_without_blank_lines_short_line_with_terminal_mark_ends_paragraph(self):
        text = f"{LONG}\n{LONG}\nкінець абзацу.\n{LONG}\n{LONG}"
        self.assertEqual(page_paragraphs(text), [f"{LONG} {LONG} кінець абзацу.", f"{LONG} {LONG}"])

    def test_short_line_without_terminal_mark_does_not_end_paragraph(self):
        self.assertEqual(page_paragraphs(f"{LONG}\nкоротко\n{LONG}"), [f"{LONG} коротко {LONG}"])

    def test_long_line_with_terminal_mark_does_not_end_paragraph(self):
        self.assertEqual(page_paragraphs(f"{LONG}.\n{LONG}"), [f"{LONG}. {LONG}"])

    def test_every_terminal_mark_counts(self):
        for mark in ".!?…:»\"”":
            with self.subTest(mark=mark):
                self.assertEqual(len(page_paragraphs(f"{LONG}\nкінець{mark}\n{LONG}")), 2)

    def test_chapter_heading_line_is_its_own_paragraph_in_line_mode(self):
        self.assertEqual(page_paragraphs(f"{LONG}\nГЛАВА 2\n{LONG}"), [LONG, "ГЛАВА 2", LONG])

    def test_hyphen_joins_still_work_across_lines(self):
        self.assertEqual(page_paragraphs("була кра-\nсивою, розмовля¬\nти"), ["була красивою, розмовляти"])

    def test_hyphen_join_across_blank_line_is_not_a_paragraph_break(self):
        self.assertEqual(page_paragraphs("була кра-\n\nсивою"), ["була красивою"])

    def test_crlf_is_a_plain_line_break(self):
        self.assertEqual(page_paragraphs("раз\r\nдва\r\n\r\nтри"), ["раз два", "три"])

    def test_page_number_line_does_not_create_a_paragraph_break(self):
        self.assertEqual(page_paragraphs("текст сторінки\n  12  \nдалі"), ["текст сторінки далі"])


class ParaStartsTest(unittest.TestCase):
    def test_para_starts_index_first_word_of_each_paragraph(self):
        book = build_book(["Раз два.\n\nТри чотири.\n\nП'ять."])
        self.assertEqual(book["para_starts"], [0, 2, 4])

    def test_paragraph_continues_on_next_page_without_terminal_mark(self):
        book = build_book(["Раз два\n\nтри чотири", "п'ять шість.\n\nСім."])
        self.assertEqual(book["words"][4:6], ["п'ять", "шість."])
        self.assertEqual(book["para_starts"], [0, 2, 6])

    def test_page_ending_with_terminal_mark_starts_new_paragraph(self):
        book = build_book(["Раз два.", "Три."])
        self.assertEqual(book["para_starts"], [0, 2])

    def test_empty_page_does_not_break_continuation(self):
        book = build_book(["раз два", "", "три."])
        self.assertEqual(book["para_starts"], [0])

    def test_no_words_no_paragraphs(self):
        self.assertEqual(build_book(["", "  "])["para_starts"], [])

    def test_para_starts_sorted_and_unique(self):
        book = build_book(["А.\n\n\n\nБ.", "", "В.\n\nГ"])
        self.assertEqual(book["para_starts"], sorted(set(book["para_starts"])))
        self.assertEqual(book["para_starts"], [0, 1, 2, 3])


class ChaptersTest(unittest.TestCase):
    PAGES = ["один два", "три чотири", "", "п'ять шість"]  # page_starts [0, 2, 4, 4]

    def test_toc_maps_pages_to_word_indices(self):
        book = build_book(self.PAGES, toc=[[1, "Перша", 1], [2, "Підрозділ", 2], [1, "Друга", 4]])
        self.assertEqual(book["chapters"], [
            {"title": "Перша", "level": 1, "word": 0},
            {"title": "Підрозділ", "level": 2, "word": 2},
            {"title": "Друга", "level": 1, "word": 4},
        ])

    def test_toc_entries_with_invalid_pages_are_skipped(self):
        book = build_book(self.PAGES, toc=[[1, "Нуль", 0], [1, "Мінус", -1], [1, "Задалеко", 9], [1, "Ок", 2]])
        self.assertEqual([c["title"] for c in book["chapters"]], ["Ок"])

    def test_toc_word_is_clamped_to_last_word(self):
        book = build_book(["раз два", ""], toc=[[1, "Кінець", 2]])  # page 2 starts at len(words)
        self.assertEqual(book["chapters"][0]["word"], 1)

    def test_toc_is_sorted_by_word_and_titles_normalised(self):
        book = build_book(self.PAGES, toc=[[1, "Друга\xa0 глава ", 4], [1, "Перша", 1]])
        self.assertEqual([(c["title"], c["word"]) for c in book["chapters"]], [("Перша", 0), ("Друга глава", 4)])

    def test_heuristic_finds_chapter_headings_without_toc(self):
        pages = ["Передмова тут.\n\nГлава 1\n\nТекст першої.", "Розділ другий\n\nТекст другої.\n\nCHAPTER IV\n\nEnd."]
        book = build_book(pages)
        self.assertEqual(book["chapters"], [
            {"title": "Глава 1", "level": 1, "word": 2},
            {"title": "Розділ другий", "level": 1, "word": 6},
            {"title": "CHAPTER IV", "level": 1, "word": 10},
        ])

    def test_heuristic_ignores_long_paragraphs_and_other_words(self):
        pages = ["Глава 1 " + "слово " * 10 + "\n\nГлавный герой.\n\nЧастина"]
        self.assertEqual(build_book(pages)["chapters"], [])

    def test_heuristic_keeps_only_first_of_a_running_header(self):
        body = "текст " * 300
        pages = [f"Глава 1 • Назва\n\n{body}", f"Глава 1 • Назва\n\n{body}", f"Глава 2\n\n{body}",
                 f"Глава 2 • Інша\n\n{body}"]
        self.assertEqual([(c["title"], c["word"]) for c in build_book(pages)["chapters"]],
                         [("Глава 1 • Назва", 0), ("Глава 2", 606)])

    def test_heuristic_drops_printed_contents_cluster(self):
        contents = "Глава 1. Вступ\n\nГлава 2. Далі\n\nГлава 3. Кінець"
        body = "текст " * 400
        pages = [contents, body, f"Глава 1\n\n{body}", f"Глава 2\n\n{body}"]  # contents, preface, chapters
        self.assertEqual([(c["title"], c["word"]) for c in build_book(pages)["chapters"]],
                         [("Глава 1", 409), ("Глава 2", 811)])

    def test_heuristic_keeps_part_next_to_its_first_chapter(self):
        body = "текст " * 400
        titles = [c["title"] for c in build_book([f"Частина I\n\nГлава 1\n\n{body}\n\nГлава 2\n\n{body}"])["chapters"]]
        self.assertEqual(titles, ["Частина I", "Глава 1", "Глава 2"])

    def test_word_heading_ending_like_a_sentence_is_body_text(self):
        body = "текст " * 400
        pages = [f"Часть людей ушла.\n\n{body}\n\nЧастина друга\n\n{body}\n\nГлава 3.\n\n{body}"]
        self.assertEqual([c["title"] for c in build_book(pages)["chapters"]], ["Частина друга", "Глава 3."])

    def test_line_mode_full_length_line_starting_like_heading_is_body_text(self):
        line = "часть тестирования системы и это хорошо поскольку определить"  # long, as body lines
        text = f"{LONG}\n{line}\n{LONG}\n{LONG}"
        self.assertEqual(len(page_paragraphs(text)), 1)

    def test_toc_wins_over_heuristic(self):
        book = build_book(["Глава 1\n\nтекст."], toc=[[1, "Зміст", 1]])
        self.assertEqual([c["title"] for c in book["chapters"]], ["Зміст"])

    def test_no_toc_no_headings_gives_empty_list(self):
        self.assertEqual(build_book(["просто текст."])["chapters"], [])


class DjvuOutlineTest(unittest.TestCase):
    def test_parses_real_djvused_output(self):
        self.assertEqual(parse_djvu_outline(DJVU_OUTLINE), [
            [1, 'Глава 1. Вступ "цитата" back\\slash', 3],
            [2, "1.1 Підрозділ", 5],
            [1, "Chapter 2", 10],
        ])

    def test_empty_output_means_no_outline(self):
        self.assertEqual(parse_djvu_outline(""), [])
        self.assertEqual(parse_djvu_outline("(bookmarks )"), [])

    def test_non_page_targets_are_skipped_but_children_kept(self):
        text = '(bookmarks ("Зовні" "http://x" ("Вкладена" "#7")) ("Погана" "#abc"))'
        self.assertEqual(parse_djvu_outline(text), [[2, "Вкладена", 7]])

    def test_malformed_input_does_not_raise(self):
        self.assertEqual(parse_djvu_outline('(bookmarks ("без кінця'), [])


class ExtractPdfTest(unittest.TestCase):
    def _pdf(self, tmp, texts, toc=None):
        path = Path(tmp) / "t.pdf"
        doc = pymupdf.open()
        for text in texts:
            doc.new_page().insert_text((72, 72), text)
        if toc:
            doc.set_toc(toc)
        doc.save(path)
        doc.close()
        return path

    def test_extracts_one_string_per_pdf_page(self):
        with tempfile.TemporaryDirectory() as tmp:
            pages = extract_pages(self._pdf(tmp, ("first page", "second page")))
        self.assertEqual(len(pages), 2)
        self.assertIn("second page", pages[1])

    def test_extract_returns_toc_with_1_based_pages(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._pdf(tmp, ("one", "two", "three"), toc=[[1, "Start", 1], [1, "Глава", 3]])
            pages, toc = extract(path)
        self.assertEqual(len(pages), 3)
        self.assertEqual(toc, [[1, "Start", 1], [1, "Глава", 3]])

    def test_pdf_text_blocks_become_paragraphs(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.pdf"
            doc = pymupdf.open()
            page = doc.new_page()
            page.insert_text((72, 72), "First paragraph line one\nline two of the first.")
            page.insert_text((72, 300), "Second paragraph here.")
            doc.save(path)
            doc.close()
            pages, _ = extract(path)
        self.assertEqual(page_paragraphs(pages[0]),
                         ["First paragraph line one line two of the first.", "Second paragraph here."])


if __name__ == "__main__":
    unittest.main()
