// Тесты чистых функций admin_util.js — офлайн, без браузера и зависимостей.
// Запуск: node --test video_ui/static/admin_util.test.js
"use strict";

const { test } = require("node:test");
const assert = require("node:assert/strict");
const { ago, fmtMs, pct, truncate, fmtTs, statusOf, qs } = require("./admin_util.js");

test("ago: секунды, минуты, часы, сутки", () => {
  assert.equal(ago(5), "5 с");
  assert.equal(ago(89), "89 с");
  assert.equal(ago(120), "2 мин");
  assert.equal(ago(5399), "90 мин");
  assert.equal(ago(7200), "2 ч");
  assert.equal(ago(259200), "3 сут");
});

test("ago: нет данных — прочерк, а не «0 с»", () => {
  assert.equal(ago(null), "—");
  assert.equal(ago(undefined), "—");
});

test("fmtMs: до секунды миллисекунды, дальше секунды", () => {
  assert.equal(fmtMs(540), "540 мс");
  assert.equal(fmtMs(999), "999 мс");
  assert.equal(fmtMs(4980), "5,0 с");
  assert.equal(fmtMs(23299), "23,3 с");
});

test("fmtMs: ноль — это значение, а не отсутствие замера", () => {
  assert.equal(fmtMs(0), "0 мс");
  assert.equal(fmtMs(null), "—");
  assert.equal(fmtMs(undefined), "—");
});

test("pct: делится на ноль без падения", () => {
  assert.equal(pct(7, 22), "31,8%");
  assert.equal(pct(0, 10), "0,0%");
  assert.equal(pct(1, 0), "—");
});

test("truncate: обрезает с многоточием, короткое не трогает", () => {
  assert.equal(truncate("короткий", 20), "короткий");
  assert.equal(truncate("абвгдеёжзи", 5), "абвг…");
  assert.equal(truncate(null, 5), "");
});

test("fmtTs: день.месяц часы:минуты", () => {
  assert.equal(fmtTs("2026-07-29T14:23:41Z"), "29.07 14:23");
  assert.equal(fmtTs(""), "—");
  assert.equal(fmtTs("битое"), "—");
});

test("statusOf: ошибка перекрывает отсутствие ответа", () => {
  // При сбое STT вопроса могло и не быть — «нет в базе» тут врало бы.
  assert.equal(statusOf({ error: "stt", answer_found: false }).cls, "s-err");
  assert.equal(statusOf({ error: null, answer_found: false }).cls, "s-gap");
  assert.equal(statusOf({ error: null, answer_found: true }).cls, "s-ok");
});

test("statusOf: отказы рубильника и проходной — не «ошибка»", () => {
  // Плановое отключение и неопознанный киоск не должны выглядеть как сбой:
  // иначе оператор побежит искать поломку там, где сработала настройка.
  assert.equal(statusOf({ error: "disabled" }).label, "точка отключена");
  assert.equal(statusOf({ error: "gate" }).label, "не пропущен");
  assert.equal(statusOf({ error: "disabled" }).cls, "s-off");
});

test("qs: пустые значения не попадают в адрес", () => {
  assert.equal(qs({ days: 7, kiosk: "", only: null, q: undefined }), "days=7");
  assert.equal(qs({ token: "a b", kiosk: "astana" }), "token=a%20b&kiosk=astana");
  assert.equal(qs({ flag: false, other: 0 }), "other=0");
});

const { langName, tone } = require("./admin_util.js");

test("statusOf: шум и тишина — не сбой и не пробел в базе", () => {
  // База не спрашивалась, поэтому «нет в базе» тут врало бы и портило список
  // «чем пополнять базу» — ровно этот баг нашёлся на живой странице 30.07.
  assert.equal(statusOf({ error: "noise" }).cls, "s-off");
  assert.equal(statusOf({ error: "empty" }).cls, "s-off");
  assert.match(statusOf({ error: "noise" }).label, /не расслышал/);
  assert.equal(statusOf({ error: "tts" }).cls, "s-err");
});

test("langName: короткие имена языков", () => {
  assert.equal(langName("russian"), "рус");
  assert.equal(langName("kazakh"), "каз");
  assert.equal(langName(null), "—");
  assert.equal(langName("english"), "english");
});

test("tone: светофор по порогам", () => {
  assert.equal(tone("fallback", 0.1), "good");
  assert.equal(tone("fallback", 0.3), "warn");
  assert.equal(tone("fallback", 0.667), "bad");
  assert.equal(tone("wait", 8700), "good");
  assert.equal(tone("wait", 15000), "warn");
  assert.equal(tone("wait", 25000), "bad");
});

test("tone: любой сбой сервиса уже не «good»", () => {
  assert.equal(tone("failures", 0), "good");
  assert.equal(tone("failures", 0.01), "warn");
  assert.equal(tone("failures", 0.2), "bad");
});

test("tone: нет данных — без цвета, а не «good»", () => {
  assert.equal(tone("fallback", null), "");
  assert.equal(tone("неизвестно", 0.5), "");
});
