import {
  splitWord, wordDelayMs, sentenceStart, nextSentenceStart,
  pageOf, nextPageStart, prevPageStart, nextStart, prevStart, chapterAt, paragraphWindow,
} from "./rsvp-core.js";

const WPM_STEP = 25;
const WPM_MIN = 100;
const WPM_MAX = 1000;
const DEFAULT_WPM = 300;
const FONT_STEP = 4;
const FONT_MIN = 24;
const FONT_MAX = 120;
const SAVE_EVERY_WORDS = 50;
const CONTEXT_WORDS = 40; // fallback preview when the book has no paragraph data
const PARAGRAPH_PREVIEW_WORDS = 120;
const OCR_POLL_MS = 1000;

const $ = (id) => document.getElementById(id);
const ui = {
  library: $("library"), reader: $("reader"), filter: $("filter"), pick: $("pick"),
  list: $("book-list"), libStatus: $("library-status"),
  title: $("book-title"), position: $("position"), back: $("back"),
  word: $("word"), wpmLabel: $("wpm-label"), context: $("context"),
  contextChapter: $("context-chapter"), contextText: $("context-text"),
  status: $("reader-status"), progress: $("progress"), marks: $("chapter-marks"),
  chapterGroup: $("chapter-group"), chapter: $("chapter"), chapterPrev: $("chapter-prev"), chapterNext: $("chapter-next"),
  paraPrev: $("para-prev"), paraNext: $("para-next"),
  ocrPanel: $("ocr-panel"), ocrStart: $("ocr-start"), ocrProgress: $("ocr-progress"), ocrText: $("ocr-text"),
  pagePrev: $("page-prev"), prev: $("prev"), wordPrev: $("word-prev"), play: $("play"),
  wordNext: $("word-next"), next: $("next"), pageNext: $("page-next"),
  wpm: $("wpm"), wpmDown: $("wpm-down"), wpmUp: $("wpm-up"),
  font: $("font"), fontDown: $("font-down"), fontUp: $("font-up"),
};

const state = {
  books: [], loading: false, book: null, words: [], pageStarts: [], approxPages: false,
  paraStarts: [], chapters: [], chapterIdx: -1,
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

// ---------- library: recent books ----------
async function loadBooks() {
  state.loading = true;
  try {
    const res = await fetch("/api/books");
    state.books = await res.json();
    return true;
  } catch (err) {
    ui.libStatus.textContent = `Сервер недоступний: ${err.message}`;
    return false;
  } finally {
    state.loading = false;
  }
}

async function showLibrary() {
  stop();
  saveProgress();
  stopOcrPoll();
  state.book = null;
  // Back in the library: drop #book=… without adding a history entry.
  if (location.hash) history.replaceState(null, "", location.pathname + location.search);
  ui.reader.hidden = true;
  ui.library.hidden = false;
  ui.libStatus.textContent = "Завантаження…";
  if (await loadBooks()) renderLibrary();
}

function renderLibrary() {
  if (state.loading) return;
  const q = ui.filter.value.trim().toLowerCase();
  const books = state.books.filter((b) => b.title.toLowerCase().includes(q));
  ui.libStatus.textContent = state.books.length
    ? `Нещодавні: ${books.length} з ${state.books.length}`
    : "Список порожній. «Відкрити книгу…» — вибрати файл; або «Відкрити за допомогою → RSVP читалка» у Провіднику.";
  ui.list.replaceChildren(...books.map((b) => {
    const li = document.createElement("li");
    const p = loadProgress(b.id);
    const pct = p && p.total ? `${Math.round((p.idx / p.total) * 100)}%` : "";
    li.innerHTML = `<span class="fmt"></span><span class="t"></span><span class="muted"></span>`
      + `<span class="pct"></span><button class="remove" title="Прибрати зі списку (файл лишається)">×</button>`;
    li.children[0].textContent = b.format;
    li.children[1].textContent = b.title;
    li.children[2].textContent = `${b.size_mb} МБ · ${(b.opened_at || "").slice(0, 10)}`;
    li.children[3].textContent = pct;
    li.children[4].addEventListener("click", (e) => { e.stopPropagation(); removeRecent(b); });
    if (b.available) {
      li.title = `Тека: ${b.folder}`;
      li.addEventListener("click", () => { location.hash = `book=${b.id}`; });
    } else {
      li.classList.add("missing");
      li.title = `Файл недоступний (переміщено або видалено). Тека: ${b.folder}`;
    }
    return li;
  }));
}

async function removeRecent(book) {
  try {
    const res = await fetch("/api/recent/remove", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ id: book.id }),
    });
    if (!res.ok) throw new Error((await res.json()).error || res.statusText);
  } catch (err) {
    ui.libStatus.textContent = `Не вдалося прибрати: ${err.message}`;
    return;
  }
  if (await loadBooks()) renderLibrary();
}

async function pickBook() {
  ui.pick.disabled = true;
  ui.libStatus.textContent = "Виберіть файл у діалозі (він може відкритися за вікном браузера)…";
  try {
    const res = await fetch("/api/pick", { method: "POST" });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || res.statusText);
    if (data.cancelled) { renderLibrary(); return; }
    location.hash = `book=${data.id}`; // hashchange opens it
  } catch (err) {
    ui.libStatus.textContent = `Не вдалося відкрити: ${err.message}`;
  } finally {
    ui.pick.disabled = false;
  }
}

// #book=<id> opens the book: library clicks, the file dialog and the Explorer launcher all land here.
async function route() {
  const id = new URLSearchParams(location.hash.slice(1)).get("book");
  if (!id) {
    if (!ui.reader.hidden) showLibrary(); // browser Back from a book
    else renderLibrary();
    return;
  }
  if (state.book?.id === id) return;
  let book = state.books.find((b) => b.id === id);
  if (!book && await loadBooks()) book = state.books.find((b) => b.id === id);
  if (!book) {
    await showLibrary();
    ui.libStatus.textContent = "Цієї книги немає у списку нещодавніх — відкрийте її ще раз.";
    return;
  }
  openBook(book);
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
  state.chapters = [];
  renderChapters();
  try {
    const res = await fetch(`/api/book/${book.id}`);
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || res.statusText);
    if (state.book !== book) return; // user went back meanwhile
    // After OCR a sparse book (comics, figures) is still readable; only a raw scan needs OCR.
    if (data.words.length === 0 || (data.no_text && !data.ocr)) {
      showScanNotice(book, data);
      return;
    }
    state.words = data.words;
    state.pageStarts = data.page_starts;
    state.paraStarts = data.para_starts ?? [];
    state.chapters = data.chapters ?? [];
    // djvutxt skips pages without text, so DjVu page numbers are approximate unless OCR'd.
    state.approxPages = book.format.startsWith("djv") && !data.ocr;
    const saved = loadProgress(book.id);
    state.idx = Math.min(saved?.idx ?? 0, state.words.length - 1);
    setWpm(saved?.wpm ?? state.wpm);
    ui.progress.max = String(state.words.length - 1);
    const failed = data.ocr_failed_pages ? ` Не розпізнано сторінок: ${data.ocr_failed_pages} — повторний OCR спробує їх ще раз.` : "";
    setStatus(data.ocr ? `Текст розпізнано OCR — можливі помилки розпізнавання.${failed}` : "");
    renderChapters();
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
  const ci = chapterAt(state.chapters, state.idx);
  if (ci !== state.chapterIdx) { // touch the select only when the chapter changes
    state.chapterIdx = ci;
    ui.chapter.value = String(ci);
  }
}

// ---------- chapters ----------
function renderChapters() {
  const chapters = state.chapters;
  state.chapterIdx = -2; // force the next render() to sync the select
  ui.chapterGroup.hidden = !chapters.length;
  const last = Math.max(state.words.length - 1, 1);
  ui.marks.replaceChildren(...chapters.map((c, i) => {
    const mark = document.createElement("span");
    mark.style.left = `${(c.word / last) * 100}%`;
    mark.title = c.title;
    mark.dataset.i = String(i);
    if (c.level > 1) mark.className = "sub";
    return mark;
  }));
  const before = new Option("— до першої глави —", "-1");
  before.disabled = true;
  ui.chapter.replaceChildren(before, ...chapters.map((c, i) =>
    new Option(`${"   ".repeat(Math.max(c.level - 1, 0))}${c.title}`, String(i))));
}

function goChapter(i) {
  const c = state.chapters[i];
  if (c) jump(c.word);
}

// ---------- context preview (on pause) ----------
function showContext() {
  let from, to;
  if (state.paraStarts.length) {
    [from, to] = paragraphWindow(state.paraStarts, state.idx, state.words.length, PARAGRAPH_PREVIEW_WORDS);
  } else {
    from = Math.max(0, sentenceStart(state.words, state.idx) - CONTEXT_WORDS / 2);
    to = Math.min(state.words.length, state.idx + CONTEXT_WORDS);
  }
  const chapter = state.chapters[chapterAt(state.chapters, state.idx)];
  ui.contextChapter.textContent = chapter ? chapter.title : "";
  ui.contextChapter.hidden = !chapter;
  ui.contextText.replaceChildren();
  for (let i = from; i < to; i++) {
    const node = document.createElement(i === state.idx ? "mark" : "span");
    node.textContent = state.words[i];
    node.dataset.i = String(i);
    ui.contextText.append(node, " ");
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
  // without paragraph data (old cache) the paragraph keys move by sentence
  paraPrev: () => (state.paraStarts.length ? jump(prevStart(state.paraStarts, state.idx)) : nav.sentencePrev()),
  paraNext: () => (state.paraStarts.length ? jump(nextStart(state.paraStarts, state.idx)) : nav.sentenceNext()),
  chapterPrev: () => state.chapters.length && jump(prevStart(state.chapters.map((c) => c.word), state.idx)),
  chapterNext: () => state.chapters.length && jump(nextStart(state.chapters.map((c) => c.word), state.idx)),
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
  setStatus(data.ocr
    ? "OCR уже виконано, але тексту не знайдено. Можна спробувати ще раз."
    : "Це скан без текстового шару. OCR розпізнає текст (≈0.6 с на сторінку, результат кешується).");
  ui.ocrPanel.hidden = false;
  ui.ocrStart.disabled = false;
  ui.ocrProgress.hidden = true;
  ui.ocrText.textContent = "";
  pollOcr(book, false); // a run may already be going, e.g. after a page reload
}

async function startOcr() {
  const book = state.book;
  if (!book) return;
  ui.ocrStart.disabled = true;
  try {
    const res = await fetch(`/api/ocr/${book.id}`, { method: "POST" });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || res.statusText);
    pollOcr(book, true);
  } catch (err) {
    ui.ocrText.textContent = `Помилка: ${err.message}`;
    ui.ocrStart.disabled = false;
  }
}

function stopOcrPoll() {
  if (state.ocrPoll) clearTimeout(state.ocrPoll);
  state.ocrPoll = null;
}

// `started`: this chain saw the run going, so "done" means fresh text to load.
// Without it a stale "done" would reopen the book in a loop.
async function pollOcr(book, started) {
  stopOcrPoll();
  if (state.book !== book) return;
  let job;
  try {
    job = await (await fetch(`/api/ocr/${book.id}`)).json();
  } catch (err) {
    ui.ocrText.textContent = `Сервер недоступний: ${err.message} — пробую ще раз…`;
    state.ocrPoll = setTimeout(() => pollOcr(book, started), OCR_POLL_MS * 3);
    return;
  }
  if (state.book !== book) return;
  if (job.state === "running") {
    ui.ocrStart.disabled = true;
    ui.ocrProgress.hidden = false;
    ui.ocrProgress.max = Math.max(job.total, 1);
    ui.ocrProgress.value = job.done;
    ui.ocrText.textContent = job.total ? `${job.done} / ${job.total} стор.` : "Готую сторінки…";
    state.ocrPoll = setTimeout(() => pollOcr(book, true), OCR_POLL_MS);
  } else if (job.state === "done" && started) {
    openBook(book);
  } else if (job.state === "error") {
    ui.ocrText.textContent = `Помилка OCR: ${job.error}`;
    ui.ocrStart.disabled = false;
  }
}

// ---------- wiring ----------
ui.filter.addEventListener("input", renderLibrary);
ui.pick.addEventListener("click", pickBook);
ui.back.addEventListener("click", showLibrary);
ui.play.addEventListener("click", toggle);
ui.pagePrev.addEventListener("click", nav.pagePrev);
ui.prev.addEventListener("click", nav.sentencePrev);
ui.wordPrev.addEventListener("click", nav.wordPrev);
ui.wordNext.addEventListener("click", nav.wordNext);
ui.next.addEventListener("click", nav.sentenceNext);
ui.pageNext.addEventListener("click", nav.pageNext);
ui.paraPrev.addEventListener("click", nav.paraPrev);
ui.paraNext.addEventListener("click", nav.paraNext);
ui.chapterPrev.addEventListener("click", nav.chapterPrev);
ui.chapterNext.addEventListener("click", nav.chapterNext);
ui.chapter.addEventListener("change", () => { goChapter(Number(ui.chapter.value)); ui.chapter.blur(); });
ui.marks.addEventListener("click", (e) => {
  if (e.target instanceof HTMLElement && e.target.dataset.i) goChapter(Number(e.target.dataset.i));
});
ui.contextText.addEventListener("click", (e) => {
  if (e.target instanceof HTMLElement && e.target.dataset.i) jump(Number(e.target.dataset.i));
});
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
  if (ui.reader.hidden || e.ctrlKey || e.metaKey || e.altKey) return; // keep browser zoom / back
  if (e.target instanceof HTMLInputElement && e.target.type !== "range") {
    if (e.key === "Enter" || e.key === "Escape") e.target.blur();
    return; // typing a number must not drive the reader
  }
  if (e.target instanceof HTMLSelectElement) {
    if (e.key === "Escape") e.target.blur();
    return; // arrows pick a chapter in the open list
  }
  const actions = {
    " ": toggle,
    ArrowLeft: e.shiftKey ? nav.wordPrev : nav.sentencePrev,
    ArrowRight: e.shiftKey ? nav.wordNext : nav.sentenceNext,
    PageUp: e.shiftKey ? nav.chapterPrev : nav.pagePrev,
    PageDown: e.shiftKey ? nav.chapterNext : nav.pageNext,
    "[": nav.paraPrev,
    "]": nav.paraNext,
    ArrowUp: () => changeWpm(WPM_STEP),
    ArrowDown: () => changeWpm(-WPM_STEP),
    "+": () => changeFont(FONT_STEP),
    "=": () => changeFont(FONT_STEP),
    "-": () => changeFont(-FONT_STEP),
    Escape: showLibrary,
  };
  // [ ] by physical key too: on a Cyrillic layout the same keys type "х" / "ї".
  const byCode = { BracketLeft: nav.paraPrev, BracketRight: nav.paraNext };
  const action = actions[e.key] ?? byCode[e.code];
  if (!action) return;
  e.preventDefault();
  action();
});
window.addEventListener("beforeunload", saveProgress);
window.addEventListener("hashchange", route);

setWpm(DEFAULT_WPM);
setFont(loadJson("rsvp:font"), false);
loadBooks().then((ok) => { if (ok) route(); }); // on failure the status line keeps the error
