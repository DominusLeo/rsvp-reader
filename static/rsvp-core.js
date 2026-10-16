// Pure RSVP logic: no DOM, tested by tests/rsvp-core.test.mjs.

const CORE_CHAR = /[\p{L}\p{N}]/u;
const SENTENCE_END = /[.!?…]+[»"”')\]]*$/u;
const CLAUSE_END = /[,;:—–-]+[»"”')\]]*$/u;

// Spritz-style table: word length -> index of the fixation letter.
// Verified against the reference video (see tests).
const ORP_BY_LENGTH = [[1, 0], [5, 1], [9, 2], [13, 3]];
const ORP_MAX = 4;

const LONG_WORD_LETTERS = 8;
const LONG_WORD_FACTOR = 1.3;
const SENTENCE_FACTOR = 2;
const CLAUSE_FACTOR = 1.5;

function coreBounds(word) {
  const chars = [...word];
  let start = 0;
  while (start < chars.length && !CORE_CHAR.test(chars[start])) start++;
  let end = chars.length;
  while (end > start && !CORE_CHAR.test(chars[end - 1])) end--;
  return { chars, start, end };
}

export function orpIndex(word) {
  const { start, end } = coreBounds(word);
  const len = end - start;
  if (len <= 0) return 0;
  const row = ORP_BY_LENGTH.find(([maxLen]) => len <= maxLen);
  return start + (row ? row[1] : ORP_MAX);
}

export function splitWord(word) {
  const chars = [...word];
  const i = orpIndex(word);
  return [chars.slice(0, i).join(""), chars[i] ?? "", chars.slice(i + 1).join("")];
}

export function isSentenceEnd(word) {
  return SENTENCE_END.test(word);
}

export function wordDelayMs(word, wpm) {
  const { start, end } = coreBounds(word);
  let factor = 1;
  if (end - start > LONG_WORD_LETTERS) factor *= LONG_WORD_FACTOR;
  if (isSentenceEnd(word)) factor *= SENTENCE_FACTOR;
  else if (CLAUSE_END.test(word)) factor *= CLAUSE_FACTOR;
  return Math.round((60000 / wpm) * factor);
}

export function sentenceStart(words, i) {
  let j = Math.min(i, words.length - 1);
  while (j > 0 && !isSentenceEnd(words[j - 1])) j--;
  return Math.max(j, 0);
}

export function nextSentenceStart(words, i) {
  let j = i;
  while (j < words.length - 1 && !isSentenceEnd(words[j])) j++;
  return Math.min(j + 1, words.length - 1);
}
