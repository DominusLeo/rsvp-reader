import { splitWord, wordDelayMs, sentenceStart, nextSentenceStart } from "./rsvp-core.js";

const WPM_STEP = 25;
const WPM_MIN = 100;
const WPM_MAX = 1000;
const DEFAULT_WPM = 300;
const SAVE_EVERY_WORDS = 50;
const CONTEXT_WORDS = 40;

const $ = (id) => document.getElementById(id);
const ui = {
  library: $("library"), reader: $("reader"), filter: $("filter"),
  list: $("book-list"), libStatus: $("library-status"),
  title: $("book-title"), position: $("position"), back: $("back"),
  word: $("word"), wpmLabel: $("wpm-label"), context: $("context"),
  status: $("reader-status"), progress: $("progress"),
  prev: $("prev"), play: $("play"), next: $("next"),
  wpm: $("wpm"), wpmValue: $("wpm-value"),
};

const state = { books: [], bookId: null, words: [], pageStarts: [], idx: 0, wpm: DEFAULT_WPM, timer: null, sinceSave: 0 };

// Progress lives in this browser only; storage may be unavailable.
function loadProgress(id) {
  try { return JSON.parse(localStorage.getItem(`rsvp:${id}`)) || null; } catch { return null; }
}
function saveProgress() {
  if (!state.bookId) return;
  try {
    localStorage.setItem(`rsvp:${state.bookId}`, JSON.stringify({
      idx: state.idx, wpm: state.wpm, total: state.words.length,
    }));
  } catch { /* private mode: progress is not kept */ }
}

// ---------- library ----------
async function showLibrary() {
  stop();
  saveProgress();
  state.bookId = null;
  ui.reader.hidden = true;
  ui.library.hidden = false;
  ui.libStatus.textContent = "Завантаження…";
  try {
    const res = await fetch("/api/books");
    state.books = await res.json();
    renderLibrary();
  } catch (err) {
    ui.libStatus.textContent = `Сервер недоступний: ${err.message}`;
  }
}

function renderLibrary() {
  const q = ui.filter.value.trim().toLowerCase();
  const books = state.books.filter((b) => b.title.toLowerCase().includes(q));
  ui.libStatus.textContent = state.books.length
    ? `Книг: ${books.length} з ${state.books.length}`
    : "Книг не знайдено. Покладіть PDF / DjVu / EPUB у теку books або додайте теку в config.json.";
  ui.list.replaceChildren(...books.map((b) => {
    const li = document.createElement("li");
    const p = loadProgress(b.id);
    const pct = p && p.total ? `${Math.round((p.idx / p.total) * 100)}%` : "";
    li.innerHTML = `<span class="fmt"></span><span class="t"></span><span class="muted"></span><span class="pct"></span>`;
    li.children[0].textContent = b.format;
    li.children[1].textContent = b.title;
    li.children[2].textContent = `${b.size_mb} МБ`;
    li.children[3].textContent = pct;
    li.title = `Тека: ${b.folder}`;
    li.addEventListener("click", () => openBook(b));
    return li;
  }));
}

// ---------- reader ----------
async function openBook(book) {
  ui.library.hidden = true;
  ui.reader.hidden = false;
  ui.title.textContent = book.title;
  ui.context.hidden = true;
  setStatus("Витягую текст… (перше відкриття DjVu/PDF може тривати до хвилини)");
  showWord("");
  state.bookId = book.id;
  state.words = [];
  try {
    const res = await fetch(`/api/book/${book.id}`);
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || res.statusText);
    if (state.bookId !== book.id) return; // user went back meanwhile
    if (data.no_text || data.words.length === 0) {
      setStatus("У цій книзі немає текстового шару (скан). Потрібне OCR-розпізнавання — ще не підтримано.");
      return;
    }
    state.words = data.words;
    state.pageStarts = data.page_starts;
    const saved = loadProgress(book.id);
    state.idx = Math.min(saved?.idx ?? 0, state.words.length - 1);
    setWpm(saved?.wpm ?? state.wpm);
    ui.progress.max = String(state.words.length - 1);
    setStatus("");
    render();
    showContext();
  } catch (err) {
    setStatus(`Помилка: ${err.message}`);
  }
}

function setStatus(text) {
  ui.status.textContent = text;
  ui.status.hidden = !text;
}

function showWord(word) {
  const [l, p, r] = splitWord(word);
  ui.word.children[0].textContent = l;
  ui.word.children[1].textContent = p;
  ui.word.children[2].textContent = r;
}

function pageOf(idx) {
  let lo = 0, hi = state.pageStarts.length - 1;
  while (lo < hi) {
    const mid = (lo + hi + 1) >> 1;
    if (state.pageStarts[mid] <= idx) lo = mid; else hi = mid - 1;
  }
  return lo + 1;
}

function render() {
  if (!state.words.length) return;
  showWord(state.words[state.idx]);
  ui.progress.value = String(state.idx);
  const pct = ((state.idx / Math.max(state.words.length - 1, 1)) * 100).toFixed(1);
  ui.position.textContent = `стор. ~${pageOf(state.idx)} / ${state.pageStarts.length} · ${pct}%`;
}

function showContext() {
  const from = Math.max(0, sentenceStart(state.words, state.idx) - CONTEXT_WORDS / 2);
  const to = Math.min(state.words.length, state.idx + CONTEXT_WORDS);
  ui.context.replaceChildren();
  for (let i = from; i < to; i++) {
    const node = i === state.idx ? document.createElement("mark") : document.createTextNode("");
    node.textContent = state.words[i];
    ui.context.append(node, " ");
  }
  ui.context.hidden = false;
}

function tick() {
  if (state.idx >= state.words.length - 1) { stop(); return; }
  state.idx += 1;
  render();
  if (++state.sinceSave >= SAVE_EVERY_WORDS) { state.sinceSave = 0; saveProgress(); }
  state.timer = setTimeout(tick, wordDelayMs(state.words[state.idx], state.wpm));
}

function play() {
  if (!state.words.length || state.timer) return;
  ui.context.hidden = true;
  ui.play.textContent = "⏸";
  state.timer = setTimeout(tick, wordDelayMs(state.words[state.idx], state.wpm));
}

function stop() {
  if (state.timer) clearTimeout(state.timer);
  state.timer = null;
  ui.play.textContent = "▶";
  if (state.words.length) { saveProgress(); showContext(); }
}

const toggle = () => (state.timer ? stop() : play());

function jump(idx) {
  state.idx = Math.max(0, Math.min(idx, state.words.length - 1));
  render();
  if (!state.timer) showContext();
}

function setWpm(value) {
  state.wpm = Math.max(WPM_MIN, Math.min(WPM_MAX, Math.round(value / WPM_STEP) * WPM_STEP));
  ui.wpm.value = String(state.wpm);
  ui.wpmValue.textContent = String(state.wpm);
  ui.wpmLabel.textContent = `${state.wpm} слів/хв`;
}

// ---------- wiring ----------
ui.filter.addEventListener("input", renderLibrary);
ui.back.addEventListener("click", showLibrary);
ui.play.addEventListener("click", toggle);
ui.prev.addEventListener("click", () => {
  // mid-sentence -> its start; already at the start -> previous sentence
  const start = sentenceStart(state.words, state.idx);
  jump(state.idx > start ? start : sentenceStart(state.words, Math.max(start - 1, 0)));
});
ui.next.addEventListener("click", () => jump(nextSentenceStart(state.words, state.idx)));
ui.progress.addEventListener("input", () => jump(Number(ui.progress.value)));
ui.wpm.addEventListener("input", () => { setWpm(Number(ui.wpm.value)); saveProgress(); });

document.addEventListener("keydown", (e) => {
  if (ui.reader.hidden || e.target === ui.filter) return;
  const actions = {
    " ": toggle,
    ArrowLeft: () => ui.prev.click(),
    ArrowRight: () => ui.next.click(),
    ArrowUp: () => setWpm(state.wpm + WPM_STEP),
    ArrowDown: () => setWpm(state.wpm - WPM_STEP),
    Escape: showLibrary,
  };
  const action = actions[e.key];
  if (!action) return;
  e.preventDefault();
  action();
});
window.addEventListener("beforeunload", saveProgress);

setWpm(DEFAULT_WPM);
showLibrary();
