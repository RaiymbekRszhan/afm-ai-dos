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
