/* Детектор речи для киоска: была ли в записи РЕЧЬ, а не шум холла.
 *
 * Зачем. Движок STT на тишине не молчит, а выдумывает текст — в логах киоска
 * 29.07 это «Редактор субтитров А.Семкин Корректор А.Кулакова», «Продолжение
 * следует.», «Тихо, тихо, тихо». Дальше система честно ищет ответ на выдуманный
 * вопрос: тратит облако, пишет промах в аналитику и показывает гражданину отказ.
 * Значит запись без речи слать нельзя.
 *
 * Первая версия проверяла один порог амплитуды (peak > 0.015) — в тихой комнате
 * работала, в фойе с людьми и кондиционером срабатывала на чём угодно: вдох,
 * стук по стойке, чужой разговор. Здесь три отличия:
 *   1. RMS, а не пик: одиночный щелчок даёт большой пик, но крошечный RMS;
 *   2. порог АДАПТИВНЫЙ — считается от фонового шума, который меряется по самым
 *      тихим моментам самой записи (в паузах между словами);
 *   3. нужен не всплеск, а НАКОПЛЕННОЕ время звучания (minVoicedMs) — короткий
 *      хлопок речью не станет.
 *
 * Чистый модуль без DOM и Web Audio: логика проверяется node-тестом (vad.test.js),
 * страница только скармливает ему RMS очередного буфера.
 */
(function (root, factory) {
  if (typeof module === "object" && module.exports) module.exports = factory();
  else root.AidosVad = factory();
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  var DEFAULTS = {
    // Абсолютный минимум: тише этого не речь даже в идеальной тишине (защита от
    // деления порога до нуля, когда микрофон почти не шумит).
    absFloor: 0.015,
    // Во сколько раз речь должна быть громче фона. 3 — компромисс: 2 ловит
    // разговоры за спиной, 5 требует кричать в микрофон.
    margin: 3,
    // Потолок порога: в очень шумном холле фон может быть высоким, но требовать
    // от гражданина крика нельзя.
    maxThreshold: 0.12,
    // Сколько НАКОПЛЕННОГО звучания считаем речью. Короткое «да» — примерно
    // 300 мс, поэтому ниже опускать нельзя, иначе согласие перестанет работать.
    minVoicedMs: 300,
  };

  function createVad(options) {
    var o = options || {};
    var absFloor = o.absFloor != null ? o.absFloor : DEFAULTS.absFloor;
    var margin = o.margin != null ? o.margin : DEFAULTS.margin;
    var maxThreshold = o.maxThreshold != null ? o.maxThreshold : DEFAULTS.maxThreshold;
    var minVoicedMs = o.minVoicedMs != null ? o.minVoicedMs : DEFAULTS.minVoicedMs;

    var noiseFloor = Infinity;   // самый тихий момент записи = фон
    var voicedMs = 0;            // сколько всего звучало громче порога
    var silentMs = 0;            // сколько тишины подряд ПОСЛЕ речи
    var totalMs = 0;

    function threshold() {
      if (noiseFloor === Infinity) return absFloor;
      return Math.max(absFloor, Math.min(noiseFloor * margin, maxThreshold));
    }

    return {
      /** Очередной буфер: rms — среднеквадратичная громкость, ms — его длительность. */
      push: function (rms, ms) {
        totalMs += ms;
        // Фон обновляем ВСЕГДА: в паузах между словами rms падает до фонового,
        // и минимум по записи — честная его оценка. Речь минимум не занижает.
        if (rms < noiseFloor) noiseFloor = rms;
        if (rms > threshold()) {
          voicedMs += ms;
          silentMs = 0;
        } else if (voicedMs > 0) {
          silentMs += ms;
        }
      },
      /** Была ли речь: накоплено достаточно звучания. */
      hasSpeech: function () { return voicedMs >= minVoicedMs; },
      /** Тишина подряд после речи — для авто-остановки записи. */
      silenceMs: function () { return silentMs; },
      /** Диагностика (показываем в статусе при отладке). */
      stats: function () {
        return { voicedMs: voicedMs, silentMs: silentMs, totalMs: totalMs,
                 noiseFloor: noiseFloor === Infinity ? null : noiseFloor,
                 threshold: threshold() };
      },
    };
  }

  /** RMS буфера Float32 (0..1). Выносим сюда, чтобы тест не тянул Web Audio. */
  function rmsOf(samples) {
    if (!samples || !samples.length) return 0;
    var sum = 0;
    for (var i = 0; i < samples.length; i++) sum += samples[i] * samples[i];
    return Math.sqrt(sum / samples.length);
  }

  return { createVad: createVad, rmsOf: rmsOf, DEFAULTS: DEFAULTS };
});
