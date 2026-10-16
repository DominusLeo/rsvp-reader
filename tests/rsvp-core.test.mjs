// Run: node --test tests/rsvp-core.test.mjs (Node 22 does not accept a directory)
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  orpIndex, splitWord, wordDelayMs, sentenceStart, nextSentenceStart,
  pageOf, nextPageStart, prevPageStart, nextStart, prevStart, chapterAt, paragraphWindow,
} from "../static/rsvp-core.js";

// paragraph starts at words 0, 4, 9; book has 15 words
const PARAS = [0, 4, 9];

test("nextStart jumps to the next start and stays put after the last one", () => {
  assert.equal(nextStart(PARAS, 0), 4);
  assert.equal(nextStart(PARAS, 5), 9);
  assert.equal(nextStart(PARAS, 9), 9);
  assert.equal(nextStart(PARAS, 12), 12);
  assert.equal(nextStart([], 7), 7);
});

test("prevStart goes to the current start first, then to the previous one", () => {
  assert.equal(prevStart(PARAS, 6), 4);
  assert.equal(prevStart(PARAS, 4), 0);
  assert.equal(prevStart(PARAS, 0), 0);
  assert.equal(prevStart([], 7), 0);
});

test("prevStart before the first start goes to it", () => {
  assert.equal(prevStart([3, 8], 1), 0);
  assert.equal(prevStart([3, 8], 3), 0);
});

test("paragraphWindow returns the whole current paragraph", () => {
  assert.deepEqual(paragraphWindow(PARAS, 5, 15, 120), [4, 9]);
  assert.deepEqual(paragraphWindow(PARAS, 12, 15, 120), [9, 15]);
  assert.deepEqual(paragraphWindow([3, 8], 1, 15, 120), [0, 3]); // before the first start
});

test("paragraphWindow trims a long paragraph around the word, a quarter before it", () => {
  assert.deepEqual(paragraphWindow([0], 500, 1000, 120), [470, 590]);
  assert.deepEqual(paragraphWindow([0], 10, 1000, 120), [0, 120]);
  assert.deepEqual(paragraphWindow([0], 990, 1000, 120), [880, 1000]);
});

test("chapterAt returns the index of the last chapter starting at or before idx", () => {
  const ch = [{ word: 5 }, { word: 20 }, { word: 20 }, { word: 40 }];
  assert.equal(chapterAt(ch, 0), -1);
  assert.equal(chapterAt(ch, 5), 0);
  assert.equal(chapterAt(ch, 19), 0);
  assert.equal(chapterAt(ch, 20), 2);
  assert.equal(chapterAt(ch, 100), 3);
  assert.equal(chapterAt([], 10), -1);
});

// pages start at word 0, 10, 10 (empty page), 25; book has 40 words
const PAGES = [0, 10, 10, 25];

test("pageOf returns the 1-based page holding the word", () => {
  assert.equal(pageOf(PAGES, 0), 1);
  assert.equal(pageOf(PAGES, 9), 1);
  assert.equal(pageOf(PAGES, 10), 3); // empty page 2 is skipped
  assert.equal(pageOf(PAGES, 39), 4);
});

test("nextPageStart skips empty pages and stays put on the last page", () => {
  assert.equal(nextPageStart(PAGES, 3), 10);
  assert.equal(nextPageStart(PAGES, 10), 25);
  assert.equal(nextPageStart(PAGES, 30), 30); // already on the last page: position kept
});

test("prevPageStart goes to the current page start, then to the previous page", () => {
  assert.equal(prevPageStart(PAGES, 30), 25);
  assert.equal(prevPageStart(PAGES, 25), 10);
  assert.equal(prevPageStart(PAGES, 10), 0);
  assert.equal(prevPageStart(PAGES, 0), 0);
});

// Expected red letters are taken from frames of the reference video.
const VIDEO_FRAMES = [
  ["к", 0], ["со", 1], ["этим", 1], ["200.", 1], ["чтобы", 1], ["толпе.", 1],
  ["всегда", 2], ["перестать", 2], ["впитывай", 2], ["внимание.", 2], ["память:", 2],
  ["понимаешь,", 2], ["Доверяй", 2], ["Осталась", 2],
  ["французский", 3], ["перечитывает.", 3], ["прочитанное.", 3],
];

test("orpIndex matches the red letter in the reference video", () => {
  for (const [word, idx] of VIDEO_FRAMES) assert.equal(orpIndex(word), idx, word);
});

test("orpIndex caps at 4 for very long words", () => {
  assert.equal(orpIndex("електрокардіографічний"), 4);
});

test("orpIndex skips leading punctuation", () => {
  assert.equal(orpIndex("«Ходімо,"), 3); // «=0, Х=1, о=2, д=3: core "Ходімо" len 6 -> 2, +1 offset
});

test("splitWord returns left / pivot / right around the ORP", () => {
  assert.deepEqual(splitWord("перестать"), ["пе", "р", "естать"]);
  assert.deepEqual(splitWord("к"), ["", "к", ""]);
});

test("wordDelayMs: base interval is 60000 / wpm", () => {
  assert.equal(wordDelayMs("слово", 300), 200);
});

test("wordDelayMs: sentence end doubles, clause punctuation adds half", () => {
  assert.equal(wordDelayMs("кінець.", 300), 400);
  assert.equal(wordDelayMs("пауза,", 300), 300);
  assert.equal(wordDelayMs("питання?»", 300), 400);
});

test("wordDelayMs: long words get extra time", () => {
  assert.equal(wordDelayMs("перечитувати", 300), 260); // 12 letters > 8 -> x1.3
});

test("sentenceStart finds the first word of the current sentence", () => {
  const w = ["Раз", "два.", "Три", "чотири", "п'ять."];
  assert.equal(sentenceStart(w, 3), 2);
  assert.equal(sentenceStart(w, 2), 2);
  assert.equal(sentenceStart(w, 1), 0);
});

test("nextSentenceStart jumps past the current sentence end", () => {
  const w = ["Раз", "два.", "Три", "чотири."];
  assert.equal(nextSentenceStart(w, 0), 2);
  assert.equal(nextSentenceStart(w, 2), 3); // no next sentence -> last word
});
