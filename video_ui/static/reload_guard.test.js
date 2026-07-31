/* Когда киоск перезагружает себя по команде сервера.
 *
 * Цена ошибки видна гражданину: перезагрузка не вовремя обрывает ответ на
 * полуслове, а зациклившаяся превращает экран в мигалку — и чинить её на точке
 * некому. Поэтому решение проверяется тестами, а не «на глаз на киоске».
 */
const test = require("node:test");
const assert = require("node:assert");
const guard = require("./reload_guard.js");

test("первая полученная версия — эталон, а не повод перезагружаться", () => {
  // Страница только что загрузилась именно с этим кодом. Иначе каждый старт
  // киоска заканчивался бы лишней перезагрузкой.
  assert.equal(guard.versionChanged(null, "abc123"), false);
  assert.equal(guard.versionChanged(undefined, "abc123"), false);
});

test("версия сменилась — перезагружаемся", () => {
  assert.equal(guard.versionChanged("abc123", "def456"), true);
});

test("версия та же — не трогаем", () => {
  assert.equal(guard.versionChanged("abc123", "abc123"), false);
});

test("сервер версию не прислал — не наше дело", () => {
  // Старый бэкенд или урезанный ответ не должны гасить страницу.
  assert.equal(guard.versionChanged("abc123", undefined), false);
  assert.equal(guard.versionChanged("abc123", ""), false);
});

test("занятую точку не перезагружаем", () => {
  // Гражданин диктует вопрос или слушает ответ — команда подождёт.
  assert.equal(guard.shouldReload({ idle: false, lastReloadAt: 0, now: 1e6 }), false);
});

test("свободную точку перезагружаем", () => {
  assert.equal(guard.shouldReload({ idle: true, lastReloadAt: 0, now: 1e6 }), true);
});

test("две перезагрузки подряд — это цикл, вторую не делаем", () => {
  const now = 1e6;
  assert.equal(guard.shouldReload({ idle: true, lastReloadAt: now - 1000, now }), false);
});

test("после выдержки перезагрузка снова разрешена", () => {
  const now = 1e6;
  assert.equal(
    guard.shouldReload({ idle: true, lastReloadAt: now - guard.GUARD_MS - 1, now }),
    true);
});

test("часы съехали назад — защита не блокирует обновление", () => {
  // Лучше перезагрузиться лишний раз, чем застрять на старом коде из-за
  // кривой метки времени в хранилище.
  const now = 1e6;
  assert.equal(guard.shouldReload({ idle: true, lastReloadAt: now + 5e6, now }), true);
});
