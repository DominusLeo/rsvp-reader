import {
  splitWord, wordDelayMs, sentenceStart, nextSentenceStart,
  pageOf, nextPageStart, prevPageStart,
} from "./rsvp-core.js";

const WPM_STEP = 25;
const WPM_MIN = 100;
const WPM_MAX = 1000;
const DEFAULT_WPM = 300;
const FONT_STEP = 4;
const FONT_MIN = 24;
const FONT_MAX = 120;
const SAVE_EVERY_WORDS = 50;
const CONTEXT_WORDS = 40;
const OCR_POLL_MS = 1000;

const $ = (id) => document.getElementById(id);
const ui = {
  library: $("library"), reader: $("reader"), filter: $("filter"), rootFilter: $("root-filter"),
  list: $("book-list"), libStatus: $("library-status"),
  title: $("book-title"), position: $("position"), back: $("back"),
  word: $("word"), wpmLabel: $("wpm-label"), context: $("context"),
  status: $("reader-status"), progress: $("progress"),
  ocrPanel: $("ocr-panel"), ocrStart: $("ocr-start"), ocrProgress: $("ocr-progress"), ocrText: $("ocr-text"),
  pagePrev: $("page-prev"), prev: $("prev"), wordPrev: $("word-prev"), play: $("play"),
  wordNext: $("word-next"), next: $("next"), pageNext: $("page-next"),
  wpm: $("wpm"), wpmDown: $("wpm-down"), wpmUp: $("wpm-up"),
  font: $("font"), fontDown: $("font-down"), fontUp: $("font-up"),
};

const state = {
  books: [], loading: false, book: null, words: [], pageStarts: [], approxPages: false,
  idx: 0, wpm: DEFAULT_WPM, timer: null, sinceSave: 0, ocrPoll: null,
};

// Progress and font live in this browser only; storage may be unavailable.
function loadJson(key) {
  try { return JSON.parse(localStorage.getItem(key)); } catch { return null; }
}
function storeJson(key, value) {
  try { localStorage.setItem(key, JSON.stringify(value)); } catch { /* private mode: not kept */ }
}
const loadProgress = (id) => loadJson(`rsvp:${id}`);
function saveProgress() {
  if (!state.book || !state.words.length) return;
  storeJson(`rsvp:${state.book.id}`, { idx: state.idx, wpm: state.wpm, total: state.words.length });
}

// ---------- library ----------
async function showLibrary() {
  stop();
  saveProgress();
  stopOcrPoll();
  state.book = null;
  ui.reader.hidden = true;
  ui.library.hidden = false;
  state.loading = true;
  ui.libStatus.textContent = "Завантаження… (сканую теки з книгами)";
  try {
    const res = await fetch("/api/books");
    state.books = await res.json();
    state.loading = false;
    fillRootFilter();
    renderLibrary();
  } catch (err) {
    state.loading = false;
    ui.libStatus.textContent = `Сервер недоступний: ${err.message}`;
  }
}

function fillRootFilter() {
  const chosen = ui.rootFilter.value;
  const roots = [...new Set(state.books.map((b) => b.root))].sort();
  ui.rootFilter.replaceChildren(new Option("Усі теки", ""), ...roots.map((r) => new Option(r, r)));
  ui.rootFilter.value = roots.includes(chosen) ? chosen : "";
}

function renderLibrary() {
  if (state.loading) return; // the list arrives with the scan
  const q = ui.filter.value.trim().toLowerCase();
  const root = ui.rootFilter.value;
  const books = state.books.filter((b) => (!root || b.root === root) && b.title.toLowerCase().includes(q));
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
    li.title = `Тека: ${b.root} / ${b.folder}`;
    li.addEventListener("click", () => openBook(b));
    return li;
  }));
}

// ---------- reader ----------
async function openBook(book) {
  stop();
  stopOcrPoll();
  ui.library.hidden = true;
  ui.reader.hidden = false;
  ui.title.textContent = book.title;
  ui.position.textContent = "";
  ui.context.hidden = true;
  ui.ocrPanel.hidden = true;
  setStatus("Витягую текст… (перше відкриття DjVu/PDF може тривати до хвилини)");
  showWord("");
  state.book = book;
  state.words = [];
  try {
    const res = await fetch(`/api/book/${book.id}`);
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || res.statusText);
    if (state.book !== book) return; // user went back meanwhile
    if (data.no_text || data.words.length === 0) {
      showScanNotice(book, data);
      return;
    }
    state.words = data.words;
    state.pageStarts = data.page_starts;
    // djvutxt skips pages without text, so DjVu page numbers are approximate unless OCR'd.
    state.approxPages = book.format.startsWith("djv") && !data.ocr;
    const saved = loadProgress(book.id);
    state.idx = Math.min(saved?.idx ?? 0, state.words.length - 1);
    setWpm(saved?.wpm ?? state.wpm);
    ui.progress.max = String(state.words.length - 1);
    setStatus(data.ocr ? "Текст розпізнано OCR — можливі помилки розпізнавання." : "");
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

function render() {
  if (!state.words.length) return;
  showWord(state.words[state.idx]);
  ui.progress.value = String(state.idx);
  const pct = ((state.idx / Math.max(state.words.length - 1, 1)) * 100).toFixed(1);
  const tilde = state.approxPages ? "~" : "";
  ui.position.textContent = `стор. ${tilde}${pageOf(state.pageStarts, state.idx)} / ${state.pageStarts.length} · ${pct}%`;
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
  if (!state.words.length) return;
  state.idx = Math.max(0, Math.min(idx, state.words.length - 1));
  render();
  if (!state.timer) { showContext(); saveProgress(); }
}

const nav = {
  pagePrev: () => jump(prevPageStart(state.pageStarts, state.idx)),
  pageNext: () => jump(nextPageStart(state.pageStarts, state.idx)),
  // mid-sentence -> its start; already at the start -> previous sentence
  sentencePrev: () => {
    const start = sentenceStart(state.words, state.idx);
    jump(state.idx > start ? start : sentenceStart(state.words, Math.max(start - 1, 0)));
  },
  sentenceNext: () => jump(nextSentenceStart(state.words, state.idx)),
  wordPrev: () => jump(state.idx - 1),
  wordNext: () => jump(state.idx + 1),
};

// ---------- speed and size ----------
function setWpm(value) {
  const v = Number.isFinite(value) ? value : DEFAULT_WPM;
  state.wpm = Math.max(WPM_MIN, Math.min(WPM_MAX, Math.round(v / WPM_STEP) * WPM_STEP));
  ui.wpm.value = String(state.wpm);
  ui.wpmLabel.textContent = `${state.wpm} слів/хв`;
}
const changeWpm = (delta) => { setWpm(state.wpm + delta); saveProgress(); };

// null = CSS default (scales with the window); a number = user-chosen px.
function setFont(px, persist = true) {
  if (px == null) {
    document.documentElement.style.removeProperty("--word-size");
    ui.font.value = String(Math.round(parseFloat(getComputedStyle(ui.word).fontSize)));
    return;
  }
  const v = Math.max(FONT_MIN, Math.min(FONT_MAX, Math.round(px)));
  document.documentElement.style.setProperty("--word-size", `${v}px`);
  ui.font.value = String(v);
  if (persist) storeJson("rsvp:font", v);
}
const changeFont = (delta) => setFont(Number(ui.font.value) + delta);

// ---------- OCR ----------
function showScanNotice(book, data) {
  if (!data.can_ocr) {
    setStatus("У цій книзі немає текстового шару, а OCR для цього формату не підтримано.");
    return;
  }
  setStatus("Це скан без текстового шару. OCR розпізнає текст (≈0.6 с на сторінку, результат кешується).");
  ui.ocrPanel.hidden = false;
  ui.ocrStart.disabled = false;
  ui.ocrProgress.hidden = true;
  ui.ocrText.textContent = "";
  pollOcr(book); // a run may already be going, e.g. after a page reload
}

async function startOcr() {
  const book = state.book;
  if (!book) return;
  ui.ocrStart.disabled = true;
  try {
    const res = await fetch(`/api/ocr/${book.id}`, { method: "POST" });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || res.statusText);
    pollOcr(book);
  } catch (err) {
    ui.ocrText.textContent = `Помилка: ${err.message}`;
    ui.ocrStart.disabled = false;
  }
}

function stopOcrPoll() {
  if (state.ocrPoll) clearTimeout(state.ocrPoll);
  state.ocrPoll = null;
}

async function pollOcr(book) {
  stopOcrPoll();
  if (state.book !== book) return;
  let job;
  try {
    job = await (await fetch(`/api/ocr/${book.id}`)).json();
  } catch (err) {
    ui.ocrText.textContent = `Сервер недоступний: ${err.message}`;
    return;
  }
  if (state.book !== book) return;
  if (job.state === "running") {
    ui.ocrStart.disabled = true;
    ui.ocrProgress.hidden = false;
    ui.ocrProgress.max = Math.max(job.total, 1);
    ui.ocrProgress.value = job.done;
    ui.ocrText.textContent = job.total ? `${job.done} / ${job.total} стор.` : "Готую сторінки…";
    state.ocrPoll = setTimeout(() => pollOcr(book), OCR_POLL_MS);
  } else if (job.state === "done") {
    openBook(book);
  } else if (job.state === "error") {
    ui.ocrText.textContent = `Помилка OCR: ${job.error}`;
    ui.ocrStart.disabled = false;
  }
}

// ---------- wiring ----------
ui.filter.addEventListener("input", renderLibrary);
ui.rootFilter.addEventListener("change", renderLibrary);
ui.back.addEventListener("click", showLibrary);
ui.play.addEventListener("click", toggle);
ui.pagePrev.addEventListener("click", nav.pagePrev);
ui.prev.addEventListener("click", nav.sentencePrev);
ui.wordPrev.addEventListener("click", nav.wordPrev);
ui.wordNext.addEventListener("click", nav.wordNext);
ui.next.addEventListener("click", nav.sentenceNext);
ui.pageNext.addEventListener("click", nav.pageNext);
ui.progress.addEventListener("input", () => jump(Number(ui.progress.value)));
ui.progress.addEventListener("change", () => ui.progress.blur()); // give keys back to the reader
ui.wpmDown.addEventListener("click", () => changeWpm(-WPM_STEP));
ui.wpmUp.addEventListener("click", () => changeWpm(WPM_STEP));
ui.wpm.addEventListener("change", () => { setWpm(Number(ui.wpm.value)); saveProgress(); });
ui.fontDown.addEventListener("click", () => changeFont(-FONT_STEP));
ui.fontUp.addEventListener("click", () => changeFont(FONT_STEP));
ui.font.addEventListener("change", () => setFont(Number(ui.font.value)));
ui.ocrStart.addEventListener("click", startOcr);

document.addEventListener("keydown", (e) => {
  if (ui.reader.hidden) return;
  if (e.target instanceof HTMLInputElement && e.target.type !== "range") {
    if (e.key === "Enter" || e.key === "Escape") e.target.blur();
    return; // typing a number must not drive the reader
  }
  const actions = {
    " ": toggle,
    ArrowLeft: e.shiftKey ? nav.wordPrev : nav.sentencePrev,
    ArrowRight: e.shiftKey ? nav.wordNext : nav.sentenceNext,
    PageUp: nav.pagePrev,
    PageDown: nav.pageNext,
    ArrowUp: () => changeWpm(WPM_STEP),
    ArrowDown: () => changeWpm(-WPM_STEP),
    "+": () => changeFont(FONT_STEP),
    "=": () => changeFont(FONT_STEP),
    "-": () => changeFont(-FONT_STEP),
    Escape: showLibrary,
  };
  const action = actions[e.key];
  if (!action) return;
  e.preventDefault();
  action();
});
window.addEventListener("beforeunload", saveProgress);

setWpm(DEFAULT_WPM);
setFont(loadJson("rsvp:font"), false);
showLibrary();
