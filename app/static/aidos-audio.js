// aidos-audio.js — проигрывание голоса Ai-dos на СТОРОНЕ ЗРИТЕЛЯ.
//
// Зачем: лицо аватара стримится зрителям через Pixel Streaming (видео), но голос
// играется на рендер-ПК и в WebRTC-поток НЕ попадает. Этот скрипт проигрывает WAV
// ответа прямо в браузере зрителя, синхронно с губами в видео.
//
// Как синхронизируемся: прямо перед началом речи Unreal шлёт в браузер по data-каналу
// Pixel Streaming сообщение-«response»  {"type":"aidos_speak","id":"<ID>"}. Оно идёт по
// той же WebRTC-связи, что и видео, поэтому приходит ~в такт появлению губ. По событию
// проигрываем заранее скачанный WAV этого id.
//
// Куда класть: подключить на СТРАНИЦЕ ПЛЕЕРА Pixel Streaming (там, где создаётся объект
// pixelStreaming), например <script src="aidos-audio.js"></script> после инициализации
// плеера. Если объект называется не window.pixelStreaming — вызвать вручную:
//   window.aidosAttachPixelStreaming(<ваш объект PixelStreaming>);
(function () {
  "use strict";

  // ============================ НАСТРОЙКИ ============================
  // Origin бэкенда Ai-dos (STT→RAG→TTS): отдаёт WAV последнего ответа и /answer/<id>.wav.
  // Пусто = ТОТ ЖЕ origin, что страница /player — а её отдаёт сам бэкенд (Path B),
  // поэтому фетчи идут same-origin и IP менять при переезде не нужно. Абсолютный URL
  // ("http://<ip>:8000") задавать только если плеер отдаётся с другого хоста, чем
  // бэкенд (тогда на /last_answer и /answer/<id>.wav нужен открытый CORS — он есть).
  const BACKEND = "";
  // Сдвиг звука относительно события губ. + = задержать звук (мс). Подбираем ВЖИВУЮ,
  // обычно 100–400 мс: если звук опережает губы — увеличить, если отстаёт — уменьшить.
  const SYNC_OFFSET_MS = 800;
  // Как часто опрашиваем бэкенд про новый ответ, чтобы скачать WAV ЗАРАНЕЕ (предзагрузка).
  const POLL_MS = 1000;
  // Сколько подготовленных ответов держим в памяти (blob-URL'ы старых освобождаем).
  const CACHE_MAX = 16;
  // Показывать ли видимую плашку «нажмите, чтобы включить звук». Выкл: разблокировку
  // делает родитель (avatar.html) по первому действию через window.aidosUnlock —
  // плашка не нужна (слушатели pointerdown/keydown на window остаются как запас).
  const SHOW_UNLOCK_OVERLAY = false;
  // ==================================================================

  function log() {
    console.log.apply(console, ["[aidos]"].concat([].slice.call(arguments)));
  }

  const prepared = new Map(); // id -> { audio: HTMLAudioElement, url: string }
  let audioUnlocked = false;  // autoplay-политика: звук доступен только после жеста
  let pendingId = null;       // событие пришло до разблокировки — сыграем после жеста

  // -------- предзагрузка WAV (ключ = id ответа) --------
  async function preload(id) {
    if (!id) return null;
    if (prepared.has(id)) return prepared.get(id);
    let resp;
    try {
      // берём ровно тот файл, что указан в событии
      resp = await fetch(BACKEND + "/answer/" + encodeURIComponent(id) + ".wav");
      if (!resp.ok) throw new Error("/answer/" + id + " -> " + resp.status);
    } catch (e) {
      // конкретного нет (кэш вытеснил / старый бэкенд без /answer) — берём последний
      log("preload fallback via /last_answer:", e.message);
      resp = await fetch(BACKEND + "/last_answer");
      if (!resp.ok) throw new Error("/last_answer -> " + resp.status);
    }
    const blob = await resp.blob();
    const url = URL.createObjectURL(blob);
    const audio = new Audio(url);
    audio.preload = "auto";
    const entry = { audio: audio, url: url };
    prepared.set(id, entry);
    // не копим память бесконечно
    while (prepared.size > CACHE_MAX) {
      const oldestId = prepared.keys().next().value;
      const old = prepared.get(oldestId);
      if (old) URL.revokeObjectURL(old.url);
      prepared.delete(oldestId);
    }
    return entry;
  }

  // -------- опрос новых ответов -> предзагрузка --------
  let lastSeenId = null;
  async function pollLoop() {
    try {
      const r = await fetch(BACKEND + "/last_answer/id");
      if (r.ok) {
        const j = await r.json();
        if (j.id && j.id !== lastSeenId) {
          lastSeenId = j.id;
          preload(j.id).catch(function (e) { log("preload err:", e.message); });
        }
      }
    } catch (e) { /* сеть моргнула — молча повторим на следующем тике */ }
    setTimeout(pollLoop, POLL_MS);
  }

  // -------- воспроизведение по id --------
  async function playById(id) {
    let entry = prepared.get(id);
    if (!entry) {
      log("id=" + id + " ещё не загружен — качаю сейчас");
      try { entry = await preload(id); } catch (e) { log("play fetch err:", e.message); return; }
    }
    if (!entry) return;
    const audio = entry.audio;
    const start = function () {
      log("playing id=" + id);
      try { audio.currentTime = 0; } catch (e) { /* до готовности метаданных бывает */ }
      audio.play().catch(function (e) { log("play() заблокирован:", e.message); });
    };
    // положительный офсет = задержать звук, чтобы попасть в такт губам
    if (SYNC_OFFSET_MS > 0) setTimeout(start, SYNC_OFFSET_MS);
    else start();
  }

  // -------- приём события «response» из Unreal --------
  function onAidos(rawMsg) {
    let data;
    try { data = (typeof rawMsg === "string") ? JSON.parse(rawMsg) : rawMsg; }
    catch (e) { return; } // не наш JSON — игнорируем
    if (!data || data.type !== "aidos_speak") return;
    log("speak event:", data);
    if (!audioUnlocked) { pendingId = data.id; log("звук ещё не разблокирован — сыграю после жеста"); return; }
    playById(data.id);
  }

  // -------- autoplay-политика: разблокировка по жесту --------
  let overlayEl = null;
  function makeOverlay() {
    const o = document.createElement("div");
    o.setAttribute("style", [
      "position:fixed", "inset:0", "z-index:2147483647",
      "display:flex", "align-items:center", "justify-content:center",
      "background:rgba(0,0,0,.6)", "color:#fff", "cursor:pointer",
      "font:600 20px/1.4 system-ui,Segoe UI,Arial,sans-serif", "text-align:center",
      "user-select:none", "-webkit-tap-highlight-color:transparent",
    ].join(";"));
    o.innerHTML = "<div>🔊 Нажмите, чтобы включить звук</div>";
    return o;
  }

  function unlockAudio() {
    if (audioUnlocked) return;
    audioUnlocked = true;
    // «прогреваем» звук тихим проигрыванием внутри жеста — так браузер снимает блок
    try {
      const AC = window.AudioContext || window.webkitAudioContext;
      if (AC) {
        const ctx = new AC();
        if (ctx.resume) ctx.resume();
        const src = ctx.createBufferSource();
        src.buffer = ctx.createBuffer(1, 1, 22050);
        src.connect(ctx.destination);
        src.start(0);
      }
    } catch (e) { /* необязательный шаг */ }
    // дополнительно «благословляем» HTMLAudio внутри жеста — часть браузеров
    // снимает блок именно на элементе <audio>, а не только на AudioContext
    try { const a = new Audio(); a.muted = true; a.play().catch(function () {}); } catch (e) {}
    log("audio unlocked");
    if (overlayEl && overlayEl.parentNode) overlayEl.parentNode.removeChild(overlayEl);
    if (pendingId) { const id = pendingId; pendingId = null; playById(id); }
  }

  function installUnlockUI() {
    // плеер PS и так требует жеста — ловим первый где угодно как запасной путь
    // (основной путь — вызов window.aidosUnlock из родителя avatar.html)
    const onceUnlock = function () { unlockAudio(); };
    window.addEventListener("pointerdown", onceUnlock, { once: true, capture: true });
    window.addEventListener("keydown", onceUnlock, { once: true, capture: true });
    if (!SHOW_UNLOCK_OVERLAY) return;  // видимую плашку не создаём
    overlayEl = makeOverlay();
    overlayEl.addEventListener("click", unlockAudio, { once: true });
    if (document.body) document.body.appendChild(overlayEl);
    else document.addEventListener("DOMContentLoaded", function () { document.body.appendChild(overlayEl); });
  }

  // -------- подключение к объекту PixelStreaming --------
  function attach(ps) {
    if (!ps || typeof ps.addResponseEventListener !== "function") return false;
    // epicgames-библиотека: слушатель data-канальных «response» с именем "aidos"
    ps.addResponseEventListener("aidos", onAidos);
    log("attached to PixelStreaming (response listener 'aidos')");
    return true;
  }
  // Позволяем подключиться вручную, если объект называется иначе:
  //   window.aidosAttachPixelStreaming(myPixelStreamingInstance)
  window.aidosAttachPixelStreaming = attach;
  // Разблокировка звука снаружи: avatar.html (тот же origin) зовёт её по первому
  // действию пользователя (кнопка «Задать вопрос» / первый тап) — плашка не нужна.
  window.aidosUnlock = unlockAudio;

  // Пытаемся найти объект автоматически (частые имена), иначе ждём ручного attach.
  (function waitForPS(tries) {
    const ps = window.pixelStreaming || window.stream || null;
    if (attach(ps)) return;
    if (tries <= 0) {
      log("объект PixelStreaming не найден автоматически — вызовите window.aidosAttachPixelStreaming(ps)");
      return;
    }
    setTimeout(function () { waitForPS(tries - 1); }, 500);
  })(60);

  installUnlockUI();
  pollLoop();
})();
