// Клиент потокового ответа (/voice/stream): разбор NDJSON и очередь кусков озвучки.
//
// Зачем поток: озвучка — самая долгая стадия (замер 27.07: ответ на 1000 симв. =
// ~6.5 c синтеза на F5, ~20 c на Spark), и раньше гражданин ждал её ЦЕЛИКОМ.
// Бэкенд отдаёт текст сразу, а звук — кусками по мере синтеза; синтез идёт
// быстрее реального времени, поэтому пока играет кусок N, кусок N+1 уже готов.
//
// Здесь — ЧИСТАЯ логика (разбор строк и состояние очереди), без DOM и Audio:
// её покрывает node-тест stream_client.test.js. Всё, что трогает плеер и видео,
// живёт в index.html и получает готовые события.
"use strict";

// Поток приходит кусками произвольной длины, и событие может быть разрезано
// посередине. Копим хвост и отдаём только ЦЕЛЫЕ строки.
function createNdjsonParser() {
  var rest = "";
  return {
    // Возвращает массив разобранных событий; битые строки пропускает (обрыв
    // ответа не должен ронять киоск — текст на экране уже есть).
    feed: function (text) {
      var out = [];
      rest += text;
      var nl;
      while ((nl = rest.indexOf("\n")) >= 0) {
        var line = rest.slice(0, nl).trim();
        rest = rest.slice(nl + 1);
        if (!line) continue;
        try { out.push(JSON.parse(line)); } catch (e) { /* битая строка — мимо */ }
      }
      return out;
    },
    // Хвост без завершающего \n (поток закончился на полуслове) — тоже пробуем.
    flush: function () {
      var line = rest.trim();
      rest = "";
      if (!line) return [];
      try { return [JSON.parse(line)]; } catch (e) { return []; }
    },
  };
}

// Очередь кусков озвучки. Плеер один (<audio>), куски играются подряд: очередь
// решает, что играть следующим и когда ответ действительно закончился.
// Караоке-подсветка в потоке не может опираться на длительность (она известна
// только для уже пришедших кусков), поэтому прогресс считается по ДОЛЕ
// СИМВОЛОВ озвучиваемого текста: бэкенд шлёт chars на кусок и speech_chars в meta.
function createChunkQueue() {
  return {
    pending: [],          // куски, ожидающие проигрывания
    playing: false,       // идёт ли сейчас воспроизведение
    finished: false,      // поток закончился (пришёл end/error)
    playedChars: 0,       // символов уже проигранных кусков
    currentChars: 0,      // символов текущего куска
    totalChars: 0,        // символов всего (из meta)

    push: function (chunk) { this.pending.push(chunk); },

    // Следующий кусок для проигрывания (или null). Учёт символов ведём здесь же,
    // чтобы прогресс не разъезжался с тем, что реально играет.
    next: function () {
      var chunk = this.pending.shift();
      if (!chunk) {
        this.playing = false;
        return null;
      }
      this.playedChars += this.currentChars;
      this.currentChars = chunk.chars || 0;
      this.playing = true;
      return chunk;
    },

    // Ответ отзвучал ПОЛНОСТЬЮ: и поток закрыт, и очередь пуста.
    done: function () {
      return this.finished && !this.pending.length && !this.playing;
    },

    // Доля прочитанного (0..1) с учётом позиции внутри текущего куска.
    progress: function (chunkFraction) {
      if (!this.totalChars) return 0;
      var within = this.currentChars * (chunkFraction || 0);
      return Math.min(1, (this.playedChars + within) / this.totalChars);
    },

    reset: function () {
      this.pending = [];
      this.playing = false;
      this.finished = false;
      this.playedChars = this.currentChars = this.totalChars = 0;
    },
  };
}

/* для node-тестов (в браузере module нет) */
if (typeof module !== "undefined" && module.exports) {
  module.exports = { createNdjsonParser: createNdjsonParser,
                     createChunkQueue: createChunkQueue };
}
