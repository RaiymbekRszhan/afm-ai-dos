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
    // ...но абсолютного порога МАЛО. Он накапливается за всю запись, а запись
    // без речи не останавливается сама (авто-стоп ждёт речь), поэтому на
    // минутном молчании набрать 300 мс звука громче порога в фойе с людьми —
    // вопрос десятков секунд. Именно так на киоск 30.07 попали «Продолжение
    // следует.» и казахская петля. Поэтому требуем ещё и ДОЛЮ от длины записи:
    // короткое «да» (0.5 с записи) проходит по абсолютному порогу, а на 25 с
    // молчания нужно уже 0.75 с речи.
    minVoicedShare: 0.03,
    // По скольким самым тихим кадрам оцениваем фон. Одного минимума мало:
    // единственный аномально тихий кадр обрушивал порог до absFloor, который
    // может оказаться НИЖЕ реального фона холла — тогда «речью» становится
    // вообще всё. Пятый по тишине кадр так не сдвинуть.
    noiseSamples: 5,
    // ...но только там, где кадров ХВАТАЕТ. На полусекундном «да» пятый по
    // тишине оказывается почти самым громким, порог задирается и речь не
    // находится вовсе. Короткие записи этой проблемой и не страдали (набрать
    // ложные 300 мс за полсекунды невозможно), поэтому до этого порога берём
    // обычный минимум.
    robustAfterMs: 2000,
  };

  function createVad(options) {
    var o = options || {};
    var absFloor = o.absFloor != null ? o.absFloor : DEFAULTS.absFloor;
    var margin = o.margin != null ? o.margin : DEFAULTS.margin;
    var maxThreshold = o.maxThreshold != null ? o.maxThreshold : DEFAULTS.maxThreshold;
    var minVoicedMs = o.minVoicedMs != null ? o.minVoicedMs : DEFAULTS.minVoicedMs;
    var minVoicedShare = o.minVoicedShare != null ? o.minVoicedShare : DEFAULTS.minVoicedShare;
    var noiseSamples = o.noiseSamples != null ? o.noiseSamples : DEFAULTS.noiseSamples;
    var robustAfterMs = o.robustAfterMs != null ? o.robustAfterMs : DEFAULTS.robustAfterMs;

    var quietest = [];           // до noiseSamples самых тихих кадров, по возрастанию
    var voicedMs = 0;            // сколько всего звучало громче порога
    var silentMs = 0;            // сколько тишины подряд ПОСЛЕ речи
    var totalMs = 0;

    // Фон = k-й по тишине кадр: одиночный провал громкости так не утягивает
    // оценку вниз. На коротких записях кадров для этого мало (см. robustAfterMs),
    // там берём минимум.
    function noiseFloor() {
      if (!quietest.length) return Infinity;
      var i = totalMs >= robustAfterMs ? quietest.length - 1 : 0;
      return quietest[i];
    }

    function noteQuiet(rms) {
      var i = 0;
      while (i < quietest.length && quietest[i] < rms) i++;
      quietest.splice(i, 0, rms);
      if (quietest.length > noiseSamples) quietest.length = noiseSamples;
    }

    function threshold() {
      var floor = noiseFloor();
      if (floor === Infinity) return absFloor;
      return Math.max(absFloor, Math.min(floor * margin, maxThreshold));
    }

    /** Сколько звучания нужно ИМЕННО ДЛЯ ЭТОЙ записи. */
    function required() {
      return Math.max(minVoicedMs, totalMs * minVoicedShare);
    }

    return {
      /** Очередной буфер: rms — среднеквадратичная громкость, ms — его длительность. */
      push: function (rms, ms) {
        totalMs += ms;
        // Фон обновляем ВСЕГДА: в паузах между словами rms падает до фонового,
        // и самые тихие кадры записи — честная его оценка. Речь их не занижает.
        noteQuiet(rms);
        if (rms > threshold()) {
          voicedMs += ms;
          silentMs = 0;
        } else if (voicedMs > 0) {
          silentMs += ms;
        }
      },
      /** Была ли речь: накоплено достаточно звучания для длины этой записи. */
      hasSpeech: function () { return voicedMs >= required(); },
      /** Тишина подряд после речи — для авто-остановки записи. */
      silenceMs: function () { return silentMs; },
      /** Диагностика (показываем в статусе при отладке). */
      stats: function () {
        var floor = noiseFloor();
        return { voicedMs: voicedMs, silentMs: silentMs, totalMs: totalMs,
                 requiredMs: required(),
                 noiseFloor: floor === Infinity ? null : floor,
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
