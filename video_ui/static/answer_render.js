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
 * Чистые функции (parseAnswer/parseNum/pickNumericColumn) вынесены отдельно и
 * экспортируются в module.exports — их гоняет node-тест без браузера.
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
      else prose = prose.concat(tbl); // одинокая строка с «|» — это ещё проза
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
  if (rows.length < 2 || width < 2) { // не таблица — вернуть как текст
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

// Главная точка входа: кладёт разобранный ответ в container.
// Возвращает true, если была хоть одна таблица (страница расширит панель).
function renderAnswer(container, text) {
  var hadTable = false;
  parseAnswer(text).forEach(function (seg) {
    if (seg.type === "text") {
      container.appendChild(el("div", "aline", seg.text));
      return;
    }
    hadTable = true;
    container.appendChild(renderTable(seg.rows));
    var numeric = pickNumericColumn(seg.rows);
    if (numeric && seg.rows.length - 1 <= CHART_MAX_ROWS) {
      container.appendChild(renderChart(seg.rows, numeric));
    }
  });
  return hadTable;
}

/* для node-тестов (в браузере module нет) */
if (typeof module !== "undefined" && module.exports) {
  module.exports = { parseAnswer: parseAnswer, parseNum: parseNum,
                     pickNumericColumn: pickNumericColumn };
}
