/* Детектор речи: node --test video_ui/static/vad.test.js
 *
 * Сценарии взяты с живого киоска (29.07): гражданин молчит, но движок STT
 * выдумывает вопрос («Продолжение следует.»), потому что запись всё равно
 * уходит на сервер. Здесь проверяем, что такие записи не считаются речью, а
 * настоящий вопрос — считается, в том числе в шумном холле.
 */
const test = require("node:test");
const assert = require("node:assert");
const { createVad, rmsOf } = require("./vad.js");

const BUF_MS = 93;   // 4096 сэмплов при 44.1 кГц — как на странице

/** Скармливает детектору `ms` миллисекунд звука постоянной громкости. */
function feed(vad, rms, ms) {
  for (let t = 0; t < ms; t += BUF_MS) vad.push(rms, BUF_MS);
}

test("тихая комната: чистая тишина речью не считается", () => {
  const vad = createVad();
  feed(vad, 0.001, 3000);
  assert.equal(vad.hasSpeech(), false);
});

test("шумный холл: ровный фон 0.03 речью не считается", () => {
  // Старый порог (peak > 0.015) здесь срабатывал и слал тишину в STT.
  const vad = createVad();
  feed(vad, 0.03, 4000);
  assert.equal(vad.hasSpeech(), false);
});

test("шумный холл: речь громче фона — распознаём", () => {
  const vad = createVad();
  feed(vad, 0.03, 500);      // фон
  feed(vad, 0.20, 1200);     // гражданин говорит
  assert.equal(vad.hasSpeech(), true);
});

test("одиночный хлопок речью не становится", () => {
  const vad = createVad();
  feed(vad, 0.02, 800);
  vad.push(0.6, BUF_MS);     // стук по стойке: громко, но 93 мс
  feed(vad, 0.02, 800);
  assert.equal(vad.hasSpeech(), false);
});

test("короткое «да» проходит (согласие на уточнение)", () => {
  const vad = createVad();
  feed(vad, 0.01, 400);
  feed(vad, 0.15, 350);      // ~350 мс речи
  assert.equal(vad.hasSpeech(), true);
});

test("тихая речь в тихой комнате проходит", () => {
  const vad = createVad();
  feed(vad, 0.002, 400);     // очень тихий фон
  feed(vad, 0.05, 800);      // говорит негромко
  assert.equal(vad.hasSpeech(), true);
});

test("порог не улетает выше потолка в очень шумном месте", () => {
  const vad = createVad();
  feed(vad, 0.09, 1000);                  // фон * 3 = 0.27 -> обрезаем до 0.12
  assert.ok(vad.stats().threshold <= 0.12);
  feed(vad, 0.3, 800);                    // гражданин говорит громко
  assert.equal(vad.hasSpeech(), true);
});

test("тишина после речи копится — по ней страница сама останавливает запись", () => {
  const vad = createVad();
  feed(vad, 0.01, 200);
  feed(vad, 0.2, 600);
  assert.equal(vad.silenceMs(), 0);
  feed(vad, 0.01, 2100);
  assert.ok(vad.silenceMs() >= 2000);
});

test("тишина ДО речи в счётчик паузы не идёт (иначе авто-стоп сразу)", () => {
  const vad = createVad();
  feed(vad, 0.001, 5000);
  assert.equal(vad.silenceMs(), 0);
});

test("rmsOf: считает среднеквадратичную громкость", () => {
  assert.equal(rmsOf(new Float32Array([0, 0, 0])), 0);
  assert.ok(Math.abs(rmsOf(new Float32Array([0.5, -0.5, 0.5, -0.5])) - 0.5) < 1e-9);
  assert.equal(rmsOf([]), 0);
});

// ---------- пропорциональный порог и устойчивый фон (правки 30.07) ----------
test("длинное молчание не становится речью по абсолютным 300 мс", () => {
  // Реальный сценарий с киоска: гражданин нажал кнопку и отошёл. Запись идёт
  // 25 с, за это время в фойе набирается больше 300 мс звука громче порога —
  // и раньше это уходило в STT, который выдумывал текст.
  const vad = createVad();
  for (let i = 0; i < 250; i++) vad.push(0.01, 100);   // 25 с тихого фона
  for (let i = 0; i < 5; i++) vad.push(0.09, 100);     // 0,5 с шума погромче
  assert.equal(vad.stats().voicedMs, 500);
  assert.ok(vad.stats().requiredMs > 500, "для 25,5 с записи нужно больше 0,5 с");
  assert.equal(vad.hasSpeech(), false);
});

test("короткое «да» по-прежнему проходит", () => {
  // 0,5 с записи: доля даёт всего 15 мс, работает абсолютный минимум 300 мс.
  const vad = createVad();
  for (let i = 0; i < 2; i++) vad.push(0.01, 100);
  for (let i = 0; i < 4; i++) vad.push(0.09, 100);
  assert.equal(vad.stats().requiredMs, 300);
  assert.equal(vad.hasSpeech(), true);
});

test("нормальный вопрос на 6 секунд проходит", () => {
  const vad = createVad();
  for (let i = 0; i < 10; i++) vad.push(0.01, 100);    // 1 с паузы
  for (let i = 0; i < 40; i++) vad.push(0.08, 100);    // 4 с речи
  for (let i = 0; i < 10; i++) vad.push(0.01, 100);    // 1 с паузы
  assert.equal(vad.hasSpeech(), true);
});

test("одиночный аномально тихий кадр не обрушивает порог", () => {
  // Раньше фон = минимум по записи: один кадр 0.0001 ронял порог до absFloor,
  // который НИЖЕ реального фона холла — и «речью» становилось вообще всё.
  const vad = createVad();
  for (let i = 0; i < 20; i++) vad.push(0.02, 100);    // фон холла 0.02
  vad.push(0.0001, 100);                               // провал громкости
  const t = vad.stats();
  assert.ok(t.noiseFloor >= 0.02, "фон не должен уехать в ноль: " + t.noiseFloor);
  assert.ok(t.threshold > 0.02, "порог обязан остаться выше фона: " + t.threshold);
});
