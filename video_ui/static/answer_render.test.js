// Тесты чистых функций answer_render.js — офлайн, без браузера и зависимостей.
// Запуск: node --test video_ui/static/answer_render.test.js
"use strict";

const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const {
  parseAnswer,
  parseNum,
  pickNumericColumn,
  extractLinks,
  wordWeight,
  detectPrintTemplates,
} = require("./answer_render.js");

test("parseAnswer: чистая проза без таблиц", () => {
  const segs = parseAnswer("Просто ответ без таблицы.");
  assert.equal(segs.length, 1);
  assert.equal(segs[0].type, "text");
});

test("parseAnswer: маркированный блок [ТАБЛИЦА]...[/ТАБЛИЦА]", () => {
  const text =
    "Ответ:\n[ТАБЛИЦА]\n| Статья | Штраф |\n| 214 | 100 МРП |\n[/ТАБЛИЦА]\nКонец.";
  const segs = parseAnswer(text);
  const table = segs.find((s) => s.type === "table");
  assert.ok(table, "должна найтись таблица");
  assert.deepEqual(table.rows[0], ["Статья", "Штраф"]);
  assert.deepEqual(table.rows[1], ["214", "100 МРП"]);
});

test("parseAnswer: незакрытый блок [ТАБЛИЦА] (LLM оборвался) — до конца текста", () => {
  const text = "До.\n[ТАБЛИЦА]\n| a | b |\n| 1 | 2 |";
  const segs = parseAnswer(text);
  assert.ok(segs.some((s) => s.type === "table"));
});

test("parseAnswer: одинокая строка с | — это проза, а не таблица", () => {
  const segs = parseAnswer("Курс: 1 USD | 450 тенге.");
  assert.equal(segs.every((s) => s.type === "text"), true);
});

test("parseNum: базовые случаи", () => {
  assert.equal(parseNum("100 МРП"), 100);
  assert.equal(parseNum("1,5%"), 1.5);
  assert.equal(parseNum("нет чисел"), null);
});

test("pickNumericColumn: находит колонку с совпадающими единицами", () => {
  const rows = [
    ["Статья", "Штраф"],
    ["214", "100 МРП"],
    ["215", "200 МРП"],
  ];
  const col = pickNumericColumn(rows);
  assert.ok(col);
  assert.equal(col.col, 1);
  assert.deepEqual(col.values, [100, 200]);
});

test("pickNumericColumn: разные единицы в колонке — графика не будет", () => {
  const rows = [
    ["Статья", "Санкция"],
    ["214", "до 3000 МРП"],
    ["215", "от 2 до 3 кратной суммы"],
  ];
  assert.equal(pickNumericColumn(rows), null);
});

test("extractLinks: curated QR_PORTALS распознаются", () => {
  const links = extractLinks("Подайте через e-Otinish или qamqor.");
  const urls = links.map((l) => l.url);
  assert.ok(urls.includes("https://eotinish.kz"));
  assert.ok(urls.includes("https://qamqor.gov.kz"));
});

test("extractLinks: реальные .kz домены из базы ловятся", () => {
  const links = extractLinks("См. kgd.gov.kz и zan.kz, а также https://kaspi.kz/pay");
  const urls = links.map((l) => l.url);
  assert.ok(urls.includes("https://kgd.gov.kz"));
  assert.ok(urls.includes("https://zan.kz"));
  assert.ok(urls.includes("https://kaspi.kz/pay"));
});

test("extractLinks: НЕ .kz домены (произвольные .com/.org) не превращаются в QR", () => {
  // Оборона от гипотетической галлюцинации RAG: не .kz зона не должна физически
  // печататься QR-кодом на экране (см. AUDIT_2026-07-20.md, находка про QR).
  const links = extractLinks("Подробнее на evil.com или sneaky.org.");
  const urls = links.map((l) => l.url);
  assert.equal(urls.some((u) => u.includes("evil.com")), false);
  assert.equal(urls.some((u) => u.includes("sneaky.org")), false);
});

test("extractLinks: дубликаты (разный регистр/слэш) схлопываются", () => {
  const links = extractLinks("zan.kz и ZAN.KZ/ и zan.kz/");
  assert.equal(links.length, 1);
});

test("wordWeight: юр-аббревиатура тяжелее по озвучке, чем на экране", () => {
  assert.ok(wordWeight("МРП") > wordWeight("да"));
});

test("detectPrintTemplates: заявление физ/юрлица", () => {
  assert.deepEqual(detectPrintTemplates("Подайте заявление в АФМ."), ["fl", "ul"]);
});

test("detectPrintTemplates: личный приём", () => {
  assert.deepEqual(detectPrintTemplates("Запишитесь на приём."), ["priem"]);
});

test("detectPrintTemplates: 'личный приём' с кириллическим суффиксом (регрессия)", () => {
  // JS \w не видит кириллицу (в отличие от Python re в app/service.py) — раньше
  // ветка "личн\w*\s+приём" была мертва для любого реального русского текста.
  assert.deepEqual(detectPrintTemplates("Обратитесь на личный приём в отделение."), ["priem"]);
});

test("detectPrintTemplates: обычный ответ без предложения печати", () => {
  assert.deepEqual(detectPrintTemplates("Штраф составляет 100 МРП."), []);
});

test("detectPrintTemplates: контракт с бэкендом (общий fixture, N9)", () => {
  // Тот же набор, что гоняет pytest над service.detect_print_templates
  // (tests/test_print_triggers.py) — обе стороны обязаны совпасть с ним, значит
  // и друг с другом. Синхронизирует фронт (answer_render.js) и бэк (service.py).
  const fixture = path.join(__dirname, "..", "..", "tests", "fixtures", "print_triggers.json");
  const cases = JSON.parse(fs.readFileSync(fixture, "utf8"));
  for (const c of cases) {
    assert.deepEqual(detectPrintTemplates(c.text), c.expect, c.note);
  }
});
