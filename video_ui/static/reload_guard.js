/* Когда киоску можно перечитать себя по команде сервера.
 *
 * Зачем. Браузер на точке открыт сутками и код страницы сам не перечитывает:
 * до этого правка в static/** доезжала до региона только с перезапуском .bat,
 * то есть обзвоном 20 городов. Теперь сервер отдаёт версию кода в ответе на
 * пинг (`/kiosk/ping`), и страница, увидев чужую, перезагружается сама.
 *
 * Решение вынесено сюда, а не оставлено в index.html, потому что цена ошибки
 * высокая и видна гражданину: перезагрузка не вовремя обрывает ответ на
 * полуслове, а зациклившаяся — превращает экран в мигалку, и чинить её на точке
 * некому. Здесь это чистые функции, которые проверяются тестами; таймеры и
 * location.reload() остаются на странице.
 */
(function (root, factory) {
  if (typeof module === "object" && module.exports) module.exports = factory();
  else root.AidosReloadGuard = factory();
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  // Чаще, чем раз в две минуты, — это уже не обновление, а цикл.
  var GUARD_MS = 120000;

  /* Версия сервера изменилась по сравнению с той, с которой мы загрузились.
   *
   * Первое известное значение — эталон, а НЕ повод перезагружаться: страница
   * только что загрузилась именно с этим кодом. Иначе каждый старт киоска
   * заканчивался бы лишней перезагрузкой.
   */
  function versionChanged(known, incoming) {
    if (!incoming) return false;          // сервер версию не прислал — не наше дело
    if (known === null || known === undefined) return false;
    return known !== incoming;
  }

  /* Можно ли перезагружаться прямо сейчас. */
  function shouldReload(opts) {
    opts = opts || {};
    // Занятую точку не трогаем: гражданин у экрана диктует вопрос или слушает
    // ответ. Она догонит, когда договорит, — команда не протухает.
    if (!opts.idle) return false;
    var last = Number(opts.lastReloadAt || 0);
    var now = Number(opts.now || 0);
    var guard = opts.guardMs === undefined ? GUARD_MS : opts.guardMs;
    // Часы съехали назад (или хранилище отдало мусор) — считаем, что защита не
    // сработала: лучше перезагрузиться лишний раз, чем застрять на старом коде.
    if (last && now >= last && now - last < guard) return false;
    return true;
  }

  return { versionChanged: versionChanged, shouldReload: shouldReload,
           GUARD_MS: GUARD_MS };
});
