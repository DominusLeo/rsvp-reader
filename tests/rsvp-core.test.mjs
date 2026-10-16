// Run: node --test tests/
import { test } from "node:test";
import assert from "node:assert/strict";
import { orpIndex, splitWord, wordDelayMs, sentenceStart, nextSentenceStart } from "../static/rsvp-core.js";

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
