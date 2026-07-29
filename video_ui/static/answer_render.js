/* Рендер ответа Ai-dos: проза + таблицы [ТАБЛИЦА]...[/ТАБЛИЦА] + бар-чарт.
 *
 * Откуда таблицы: RAG (правило 7 промпта) прикладывает к устному ответу блок
 * с данными «строго из базы» (штрафы по категориям, пороги, сроки). Голос его
 * не читает (бэкенд вырезает, см. app/clients/tts.py strip_display_blocks),
 * а этот файл показывает: таблицей и, если колонка числовая, — графиком.
 *
 * Всё самодостаточно (ноль внешних библиотек): сеть АФМ без интернета.
 * DOM строим ТОЛЬКО через createElement/textContent — текст приходит из LLM,
 * innerHTML был бы XSS на публичном экране.
 *
 * Чистые функции (parseAnswer/parseNum/pickNumericColumn/extractLinks/...)
 * вынесены отдельно и экспортируются в module.exports — их гоняет node-тест
 * без браузера: answer_render.test.js (node --test video_ui/static/answer_render.test.js).
 */

// ---------- парсинг ----------

// Маркерный блок таблицы. [КЕСТЕ] — на случай, если LLM переведёт маркер на
// казахский; незакрытый блок (LLM оборвался) — до конца текста.
var TABLE_BLOCK_RE = /\[(ТАБЛИЦА|КЕСТЕ)\]([\s\S]*?)(?:\[\/\1\]|$)/gi;

// Текст -> сегменты [{type:"text", text} | {type:"table", rows:[[ячейки]]}].
// Ловим и «бесхозные» таблицы: 2+ подряд строки с «|» без маркеров.
function parseAnswer(text) {
  var raw = [];
  var last = 0, m;
  TABLE_BLOCK_RE.lastIndex = 0;
  while ((m = TABLE_BLOCK_RE.exec(text)) !== null) {
    if (m.index > last) raw.push({ type: "text", text: text.slice(last, m.index) });
    raw.push({ type: "tableSrc", text: m[2] });
    last = TABLE_BLOCK_RE.lastIndex;
  }
  if (last < text.length) raw.push({ type: "text", text: text.slice(last) });

  var out = [];
  raw.forEach(function (seg) {
    if (seg.type === "tableSrc") { pushTable(out, seg.text); return; }
    var prose = [], tbl = [];
    function flushProse() {
      var t = prose.join("\n").trim();
      if (t) out.push({ type: "text", text: t });
      prose = [];
    }
    function flushTbl() {
      if (tbl.length >= 2) { flushProse(); pushTable(out, tbl.join("\n")); }
      // Одинокая строка с «|» — это ещё проза, но саму палку показывать незачем:
      // она осталась от разметки таблицы, а не написана для чтения.
      else prose = prose.concat(tbl.map(function (l) {
        return l.replace(/\s*\|\s*$/, "").replace(/^\s*\|\s*/, "");
      }));
      tbl = [];
    }
    seg.text.split("\n").forEach(function (line) {
      if (line.indexOf("|") !== -1) tbl.push(line);
      else { flushTbl(); prose.push(line); }
    });
    flushTbl();
    flushProse();
  });
  return out;
}

// Кусок текста с «|»-строками -> сегмент-таблица (или текст, если не похоже).
function pushTable(out, src) {
  var rows = [];
  src.split("\n").forEach(function (line) {
    line = line.trim();
    if (!line || line.indexOf("|") === -1) return;
    if (/^[\s|:\-–—=]+$/.test(line)) return; // markdown-разделитель |---|---|
    var cells = line.split("|").map(function (c) { return c.trim(); });
    while (cells.length && cells[0] === "") cells.shift();       // ведущий «|»
    while (cells.length && cells[cells.length - 1] === "") cells.pop();
    if (cells.length) rows.push(cells);
  });
  var width = rows.reduce(function (w, r) { return Math.max(w, r.length); }, 0);
  // Одна колонка — это ПЕРЕЧЕНЬ (список субъектов финмониторинга, документов),
  // и он вполне себе таблица: заголовок + строки. Раньше здесь стояло width < 2,
  // и такой ответ вываливался на экран сырым текстом с торчащими «|» (замечено
  // на казахском ответе про субъектов 29.07). Требование «2+ строк» остаётся:
  // одинокая строка с «|» — всё ещё проза.
  if (rows.length < 2) {
    var t = src.trim();
    if (t) out.push({ type: "text", text: t });
    return;
  }
  rows.forEach(function (r) { while (r.length < width) r.push(""); });
  // Колонка, пустая во ВСЕХ строках данных, — мусор от LLM (заголовок без
  // содержимого): выкидываем, пока остаётся хотя бы одна колонка.
  for (var c = width - 1; c >= 0 && width > 1; c--) {
    var used = rows.slice(1).some(function (r) { return r[c] !== ""; });
    if (!used) { rows.forEach(function (r) { r.splice(c, 1); }); width--; }
  }
  out.push({ type: "table", rows: rows });
}

// «100 МРП», «1 000 000 тенге», «1,5%» -> число (для длины бара). null — не число.
function parseNum(cell) {
  var m = /-?\d[\d\s  ]*(?:[.,]\d+)?/.exec(cell || "");
  if (!m) return null;
  var s = m[0].replace(/[\s  ]/g, "").replace(",", ".");
  var v = parseFloat(s);
  return isFinite(v) ? v : null;
}

var _NUM_G = /-?\d[\d\s  ]*(?:[.,]\d+)?/g;
var _UNIT_STOP = { "до": 1, "от": 1, "не": 1, "более": 1, "менее": 1,
                   "свыше": 1, "около": 1, "примерно": 1, "дейін": 1, "бастап": 1 };

// Ячейка -> {v, unit}: v — число для длины бара (у диапазона «от 2 до 3» —
// ВЕРХНЯЯ граница), unit — текст ячейки без чисел и слов-ограничителей
// («до 3000 МРП» -> «мрп»). null — чисел нет.
function parseCellValue(cell) {
  var nums = (cell || "").match(_NUM_G);
  if (!nums) return null;
  var vals = nums.map(function (s) {
    return parseFloat(s.replace(/[\s  ]/g, "").replace(",", "."));
  }).filter(isFinite);
  if (!vals.length) return null;
  // Слова-ограничители не входят в «единицу»: «до 100 МРП» и «200 МРП» — одно
  // и то же. ВАЖНО: не через \b — в JS-regex без флага u он не видит кириллицу.
  var unit = (cell || "").replace(_NUM_G, " ").toLowerCase()
    .split(/[\s  ]+/)
    .filter(function (w) { return w && !_UNIT_STOP[w]; })
    .join(" ");
  return { v: Math.max.apply(null, vals), unit: unit };
}

// Первая колонка (кроме нулевой — это подписи), где ЧИСЛО есть в каждой строке
// данных И ЕДИНИЦЫ СОВПАДАЮТ. Бары сравнимы только в одних единицах: «до 3000
// МРП» против «от 2 до 3 кратной суммы неуплаты» рисовать нельзя — длины баров
// (3000 против 3) дадут гражданину ложную картину (живой случай 2026-07-17).
// Нет такой колонки — графика не будет (таблицы достаточно).
function pickNumericColumn(rows) {
  var data = rows.slice(1);
  if (data.length < 2) return null;
  for (var c = 1; c < rows[0].length; c++) {
    var cells = data.map(function (r) { return parseCellValue(r[c]); });
    if (cells.some(function (x) { return x === null || x.v < 0; })) continue;
    if (!cells.some(function (x) { return x.v > 0; })) continue;
    var unit = cells[0].unit;
    if (!cells.every(function (x) { return x.unit === unit; })) continue;
    return { col: c, values: cells.map(function (x) { return x.v; }) };
  }
  return null;
}

// ---------- когда предлагать печать образца заявления ----------
// По смыслу ответа решаем, нужны ли гражданину бумажные образцы (подать
// заявление/жалобу физ- или юрлицом; записаться на личный приём). Триггеры — по
// словам ответа, как у QR-порталов: без изменений в бэкенде/RAG. Возвращаем
// id-шники образцов ("fl"/"ul"/"priem") в порядке показа; пусто — печати нет.
// Печатает их video_ui/static/templates/pdf/{id}-{lang}.pdf (см. doPrint в index.html).
// Синхронно с service.py::_PRINT_RE_APP на бэкенде (N9); контракт сверяет
// tests/fixtures/print_triggers.json (pytest + node гоняют один и тот же набор).
var PRINT_RE_APP = /заявлени|обращени|жалоб|пожалов|ходатайств|подать|подаёт|подач|арыз|шағым|өтініш/i;
// «Приём» — только личный приём. Голое каз. «қабылдау» не берём (значит и
// «принять» — шешім/заң қабылдау). Синхронно с service.py на бэкенде — НО:
// в JS (в отличие от Python re, где \w Unicode-aware) \w — это [A-Za-z0-9_],
// кириллицу не видит. "личн\w*" на "личный"/"личного" всегда матчил \w* пустой
// строкой и требовал пробел СРАЗУ после "личн" — с реальными словами это никогда
// не совпадало (ветка была мертва). [а-яёА-ЯЁ]* — рабочий эквивалент \w* для
// кириллических суффиксов. Эта функция — fallback ТОЛЬКО для demo-режима без
// бэкенда (see index.html updatePrintButton); в бою решает X-Print backend'а
// (app/service.py, где \w для кириллицы работает как задумано).
var PRINT_RE_PRIEM = /личн[а-яёА-ЯЁ]*\s+при[еёи]м|на\s+при[еёи]м|запис[а-яёА-ЯЁ]*[^.]{0,40}при[еёи]м|при[еёи]м[а-яёА-ЯЁ]*\s+граждан|жеке\s+қабылдау|қабылдауға\s+жаз/i;

function detectPrintTemplates(text) {
  var t = text || "", out = [];
  if (PRINT_RE_APP.test(t)) { out.push("fl"); out.push("ul"); }
  if (PRINT_RE_PRIEM.test(t)) out.push("priem");
  return out;
}

// ---------- ссылки -> QR-коды ----------
// Просьба руководителя (2026-07-17): упомянул ответ портал (e-Otinish, qamqor…)
// — на экране QR-код, чтобы гражданин снял его телефоном. Именованные порталы —
// словарь с ПРОВЕРЕННЫМИ адресами (LLM url не пишет, только название); явные
// домены/URL из текста ловятся регекспом. Генерация QR — vendored qrcode.js
// (MIT, офлайн — сеть АФМ без интернета).

// [регексп упоминания, канонический URL, подпись под QR]
var QR_PORTALS = [
  [/\be-?otinish\b|е[- ]?отиниш/i, "https://eotinish.kz", "eotinish.kz"],
  [/\bqamqor(?:\.gov\.kz)?\b|камкор/i, "https://qamqor.gov.kz", "qamqor.gov.kz"],
  [/\be-?gov\b|\begov\.kz\b|е-?гов/i, "https://egov.kz", "eGov.kz"],
  [/сайте?\s+агентства|\bсайт\s+афм\b/i,
   "https://www.gov.kz/memleket/entities/afm", "Сайт АФМ"],
];
// Явный URL или голый домен с путём (минимум одна точка + известная зона).
// Только .kz (вся реальная база rag/data/*.md ссылается исключительно на .kz —
// govtec.kz, kaspi.kz, kgd.gov.kz, palata.kz, qamqor.gov.kz, uzs.gov.kz, zan.kz).
// .com/.org раньше тоже матчились — если антигаллюцинация RAG когда-нибудь даст
// сбой и модель выдумает домен, этот более широкий список превратил бы его в
// физически сканируемый QR на экране. Сужение до .kz не меняет наблюдаемое
// поведение (ни один реальный домен в базе не .com/.org), но убирает эту дыру.
var QR_URL_RE = /https?:\/\/(?:[a-z0-9-]+\.)+kz(?:\/[^\s«»"'()<>]*)?|\b(?:[a-z0-9-]+\.)+kz(?:\/[^\s«»"'()<>]*)?/gi;

function extractLinks(text) {
  var found = [], seen = {};
  function push(url, label) {
    var key = url.replace(/\/+$/, "").toLowerCase();
    if (seen[key]) return;
    seen[key] = true;
    found.push({ url: url, label: label });
  }
  QR_PORTALS.forEach(function (p) {
    if (p[0].test(text)) push(p[1], p[2]);
  });
  (text.match(QR_URL_RE) || []).forEach(function (m) {
    var url = m.replace(/[.,;:!?]+$/, "");           // знак конца предложения
    var label = url.replace(/^https?:\/\//, "").replace(/\/.*$/, "");
    push(/^https?:/.test(url) ? url : "https://" + url, label);
  });
  return found;
}

function renderQrRow(links) {
  var row = el("div", "qr-row");
  links.forEach(function (l) {
    var card = el("div", "qr-card");
    try {
      var qr = qrcode(0, "M");   // 0 = автоверсия под длину URL
      qr.addData(l.url);
      qr.make();
      var img = document.createElement("img");
      img.src = qr.createDataURL(4, 8);   // данные наши (словарь/URL) — не HTML
      img.alt = l.url;
      card.appendChild(img);
    } catch (e) { return; }               // не вышло — просто без этого QR
    card.appendChild(el("div", "qr-label", l.label));
    row.appendChild(card);
  });
  return row.children.length ? row : null;
}

// ---------- рендер (браузер) ----------

var CHART_MAX_ROWS = 10;   // больше строк — только таблица, бар-чарт нечитаем
var BAR_COLOR = "#5b7cf7"; // прошёл валидатор: контраст ≥3:1 на карточке #1e293b

function el(tag, cls, text) {
  var d = document.createElement(tag);
  if (cls) d.className = cls;
  if (text !== undefined) d.textContent = text;
  return d;
}

function renderTable(rows) {
  // 4+ колонок — «плотный» режим (мельче шрифт/отступы, см. CSS), иначе широкая
  // таблица упирается в край карточки. Обёртка со скроллом — страховка: даже
  // если и это не помогло, таблицу можно прокрутить, а не гадать по обрезку.
  var table = el("table", rows[0].length >= 4 ? "aidos-table dense" : "aidos-table");
  var thead = document.createElement("thead");
  var trh = document.createElement("tr");
  rows[0].forEach(function (h) { trh.appendChild(el("th", "", h)); });
  thead.appendChild(trh);
  table.appendChild(thead);
  var tbody = document.createElement("tbody");
  rows.slice(1).forEach(function (r) {
    var tr = document.createElement("tr");
    r.forEach(function (c) { tr.appendChild(el("td", "", c)); });
    tbody.appendChild(tr);
  });
  table.appendChild(tbody);
  var wrap = el("div", "aidos-tablewrap");
  wrap.appendChild(table);
  return wrap;
}

function svgEl(tag, attrs) {
  var e = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (var k in attrs) e.setAttribute(k, attrs[k]);
  return e;
}

function truncate(s, n) { return s.length > n ? s.slice(0, n - 1) + "…" : s; }

// Горизонтальный бар-чарт по числовой колонке. Одна серия — легенда не нужна,
// имя серии несёт подпись над графиком (заголовок числовой колонки). Значения
// подписаны у концов баров ТЕКСТОМ ИЗ ЯЧЕЙКИ (как в базе), цветом текста, не
// цветом серии. Плоское основание, скруглённый конец бара.
function renderChart(rows, numeric) {
  var data = rows.slice(1);
  var W = 720, labelW = 250, valueW = 150, barH = 20, gap = 12, padTop = 26;
  var plotW = W - labelW - valueW;
  var H = padTop + data.length * (barH + gap) - gap + 6;
  var max = Math.max.apply(null, numeric.values);

  var svg = svgEl("svg", { class: "aidos-chart", viewBox: "0 0 " + W + " " + H,
                           role: "img" });
  // подпись серии = заголовок числовой колонки (+ заголовок подписей для ясности)
  var caption = svgEl("text", { x: 0, y: 14, fill: "#94a3b8", "font-size": 13 });
  caption.textContent = rows[0][numeric.col] + " — " + rows[0][0];
  svg.appendChild(caption);

  data.forEach(function (r, i) {
    var y = padTop + i * (barH + gap);
    var v = numeric.values[i];
    var w = max > 0 ? Math.max(1, (v / max) * plotW) : 1;

    var lab = svgEl("text", { x: labelW - 8, y: y + barH / 2 + 4,
                              "text-anchor": "end", fill: "#cbd5e1", "font-size": 13 });
    lab.textContent = truncate(r[0], 30);
    var labTitle = svgEl("title", {});
    labTitle.textContent = r[0] + ": " + r[numeric.col];
    lab.appendChild(labTitle);
    svg.appendChild(lab);

    var rr = Math.min(4, w); // скругление только на «конце данных», основание плоское
    var bar = svgEl("path", {
      d: "M" + labelW + " " + y + " h" + (w - rr) +
         " a" + rr + " " + rr + " 0 0 1 " + rr + " " + rr +
         " v" + (barH - 2 * rr) +
         " a" + rr + " " + rr + " 0 0 1 " + (-rr) + " " + rr +
         " h" + (rr - w) + " z",
      fill: BAR_COLOR,
    });
    var barTitle = svgEl("title", {});
    barTitle.textContent = r[0] + ": " + r[numeric.col];
    bar.appendChild(barTitle);
    svg.appendChild(bar);

    var val = svgEl("text", { x: labelW + w + 8, y: y + barH / 2 + 4,
                              fill: "#e2e8f0", "font-size": 13, "font-weight": 600 });
    val.textContent = truncate(r[numeric.col], 18);
    svg.appendChild(val);
  });
  return svg;
}

// ---------- «караоке»: веса слов для подсветки по ходу озвучки ----------
// Точных таймингов слов F5 не отдаёт, но его темп детерминирован длиной
// ОЗВУЧИВАЕМОГО текста (~0.054 c/симв., замер 2026-07-17). Поэтому позицию
// считаем долей накопленного веса: вес слова ≈ сколько символов оно занимает
// В РЕЧИ (не на экране!) — цифры и юр-аббревиатуры раскрываются голосом в
// длинные слова, а знак препинания добавляет вес паузы.
var W_ABBR = {  // на экране -> примерная длина в речи (символов)
  "мрп": 30,      // «месячных расчётных показателей»
  "рк": 22,       // «Республики Казахстан»
  "коап": 45, "ук": 18, "упк": 34, "гк": 20, "нк": 18,
  "под/фт": 60, "од/фт": 45, "сфм": 32, "афм": 8, "тг": 6,
};
var W_PER_DIGIT = 6;     // «450» -> «четыреста пятьдесят» ~ 6 симв. на цифру
var W_SENT_PAUSE = 7;    // пауза после .!?… (~0.35 c) в «символах»
var W_CLAUSE_PAUSE = 3;  // после ,;:—

function wordWeight(token) {
  var clean = token.toLowerCase().replace(/[«»"'().!?…,;:]+/g, "");
  var w = W_ABBR[clean];
  if (w === undefined) {
    var digits = (clean.match(/\d/g) || []).length;
    w = (clean.length - digits) + digits * W_PER_DIGIT;
  }
  var tail = token.slice(-1);
  if (".!?…".indexOf(tail) !== -1) w += W_SENT_PAUSE;
  else if (",;:—".indexOf(tail) !== -1) w += W_CLAUSE_PAUSE;
  return Math.max(1, w);
}

// Проза -> div.aline из спанов-слов с data-w (весом) — страница подсвечивает
// их по ходу звука. Только createElement/textContent (XSS).
function renderProse(text) {
  var d = el("div", "aline");
  text.split(/(\s+)/).forEach(function (tok) {
    if (!tok) return;
    if (/^\s+$/.test(tok)) { d.appendChild(document.createTextNode(tok)); return; }
    var s = el("span", "w", tok);
    s.dataset.w = wordWeight(tok);
    d.appendChild(s);
  });
  return d;
}

// Главная точка входа: кладёт разобранный ответ в container.
// qrContainer (необязателен) — куда положить QR-коды. Если задан (киоск: overlay
// поверх видео, на груди аватара), QR идут туда; иначе — под ответом (старое
// поведение / dev). Возвращает true, если в container попал «богатый» контент
// (таблица) — страница расширит панель. QR в overlay на richness НЕ влияет:
// ответ-отказ «обратитесь на eotinish.kz» остаётся обычным текстом.
function renderAnswer(container, text, qrContainer) {
  var rich = false;
  parseAnswer(text).forEach(function (seg) {
    if (seg.type === "text") {
      container.appendChild(renderProse(seg.text));
      return;
    }
    rich = true;
    container.appendChild(renderTable(seg.rows));
    var numeric = pickNumericColumn(seg.rows);
    if (numeric && seg.rows.length - 1 <= CHART_MAX_ROWS) {
      container.appendChild(renderChart(seg.rows, numeric));
    }
  });
  // QR-коды упомянутых порталов/ссылок. В overlay (qrContainer) — очищаем и
  // показываем/прячем его классом .on; без overlay — рядом под ответом.
  var qrTarget = qrContainer || container, hasQr = false;
  if (qrContainer) qrContainer.textContent = "";
  if (typeof qrcode !== "undefined") {
    var links = extractLinks(text);
    if (links.length) {
      var row = renderQrRow(links);
      if (row) { qrTarget.appendChild(row); hasQr = true; }
    }
  }
  if (qrContainer) { qrContainer.classList.toggle("on", hasQr); return rich; }
  return rich || hasQr;
}

// ---------- темп проговаривания по TTS-провайдеру (N7) ----------
// Темп зависит от ДВИЖКА, а не от языка: Spark (офлайн-фолбэк казахского) говорит
// медленно — ускоряем плеером; ElevenLabs (дефолтный kk) и F5 (ru) — в норме.
// Разгон через playbackRate ничего не стоит (звук уже готов) и не роняет синтез,
// в отличие от ручки SPARK_SPEED. Провайдер приходит в теле /voice (data.provider).
var SPEECH_RATE_BY_PROVIDER = { spark: 1.3 };
function speechRate(provider) {
  return SPEECH_RATE_BY_PROVIDER[provider] || 1.0;
}

/* для node-тестов (в браузере module нет) */
if (typeof module !== "undefined" && module.exports) {
  module.exports = { parseAnswer: parseAnswer, parseNum: parseNum,
                     pickNumericColumn: pickNumericColumn,
                     extractLinks: extractLinks, wordWeight: wordWeight,
                     detectPrintTemplates: detectPrintTemplates,
                     speechRate: speechRate };
}
