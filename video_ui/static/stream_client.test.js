// Тесты чистой логики stream_client.js — офлайн, без браузера и зависимостей.
// Запуск: node --test video_ui/static/stream_client.test.js
"use strict";

const { test } = require("node:test");
const assert = require("node:assert/strict");
const { createNdjsonParser, createChunkQueue } = require("./stream_client.js");

test("ndjson: события собираются из кусков произвольной длины", () => {
  const p = createNdjsonParser();
  // Сетевой пакет может разрезать строку где угодно — событие не должно потеряться.
  assert.deepEqual(p.feed('{"type":"me'), []);
  assert.deepEqual(p.feed('ta","answer":"Ответ"}\n'),
                   [{ type: "meta", answer: "Ответ" }]);
  const two = p.feed('{"type":"audio","seq":0}\n{"type":"audio","seq":1}\n');
  assert.deepEqual(two.map(e => e.seq), [0, 1]);
});

test("ndjson: битая строка пропускается, поток продолжается", () => {
  const p = createNdjsonParser();
  const out = p.feed('не json\n{"type":"end"}\n');
  assert.deepEqual(out, [{ type: "end" }]);
});

test("ndjson: хвост без перевода строки разбирается на flush", () => {
  const p = createNdjsonParser();
  assert.deepEqual(p.feed('{"type":"end"}'), []);   // строка ещё не закрыта
  assert.deepEqual(p.flush(), [{ type: "end" }]);
  assert.deepEqual(p.flush(), []);                  // хвост уже съеден
});

test("очередь: куски выдаются по порядку и считают символы", () => {
  const q = createChunkQueue();
  q.totalChars = 300;
  q.push({ seq: 0, chars: 100 });
  q.push({ seq: 1, chars: 200 });

  assert.equal(q.next().seq, 0);
  assert.equal(q.progress(0), 0);        // начало первого куска
  assert.equal(q.progress(0.5), 100 * 0.5 / 300);

  assert.equal(q.next().seq, 1);
  assert.equal(q.progress(0), 100 / 300);  // первый кусок целиком прочитан
  assert.equal(q.progress(1), 1);
});

test("очередь: пустая очередь останавливает воспроизведение", () => {
  const q = createChunkQueue();
  q.push({ seq: 0, chars: 10 });
  q.next();
  assert.equal(q.playing, true);
  assert.equal(q.next(), null);
  assert.equal(q.playing, false);
});

test("очередь: ответ закончен только когда поток закрыт И очередь пуста", () => {
  const q = createChunkQueue();
  q.push({ seq: 0, chars: 10 });
  assert.equal(q.done(), false);         // поток ещё идёт
  q.finished = true;
  assert.equal(q.done(), false);         // кусок ещё не сыгран — не идти в idle
  q.next();
  assert.equal(q.done(), false);         // играет
  assert.equal(q.next(), null);
  assert.equal(q.done(), true);
});

test("очередь: прогресс без meta не делит на ноль", () => {
  const q = createChunkQueue();
  assert.equal(q.progress(0.5), 0);
});

test("очередь: reset готовит к следующему вопросу", () => {
  const q = createChunkQueue();
  q.totalChars = 100;
  q.push({ seq: 0, chars: 50 });
  q.next();
  q.finished = true;
  q.reset();
  assert.deepEqual(q.pending, []);
  assert.equal(q.playing, false);
  assert.equal(q.finished, false);
  assert.equal(q.progress(1), 0);
});
