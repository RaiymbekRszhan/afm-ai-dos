# Пилот Ai-dos — инструкция (бэкенд на Linux + фронтенд/аватар)

Пилотный запуск «цифрового офицера» АФМ. Этот файл — **сквозной runbook**: как поднять
бэкенд на Linux и как завести фронтенд (киоск + говорящий аватар через Pixel Streaming),
со всеми граблями. Установка бэкенда по шагам — в [DEPLOY.md](DEPLOY.md); здесь ссылки на
него, а не дубли. Контракт аватар-клиента (Unreal) — в [AVATAR.md](AVATAR.md).

---

## 0. Топология пилота

```
 ┌───────────────────────────┐        ┌──────────────────────────────┐
 │  Киоск (браузер, DS-65T)  │        │  Render-ПК (Windows, RTX)     │
 │  /avatar?kiosk=1          │        │  Unreal + Pixel Streaming     │
 │  ├ iframe → /player ──────┼──WebRTC┤  сигналинг+player.js на :80   │
 │  │  (видео + aidos-audio) │  видео │  id стримера = Editor         │
 │  └ микрофон → /voice      │◄──────►│  data-канал: aidos_speak      │
 └─────────────┬─────────────┘        └───────────────┬──────────────┘
               │ HTTP (same-origin)                   │ GET /last_answer* (WAV+X-Id)
               ▼                                       ▼
        ┌──────────────────────────── Linux-сервер (GPU) ───────────────────────────┐
        │  :8000 оркестратор  ·  :8077 RAG  ·  :8809 Spark(kk)  ·  ru-TTS с АФМ :8991 │
        └───────────────────────────────────────────────────────────────────────────┘
                     │ LLM 192.168.165.2:8901 · эмбеддинги :8806 · STT ru :8804
```

Три машины: **Linux-сервер** (весь бэкенд), **Render-ПК Windows** (Unreal рисует и
стримит лицо), **киоск-браузер** (показывает видео + пишет микрофон). Голос ответа
играет **браузер киоска** синхронно с губами (звук в WebRTC-поток из Unreal не попадает).

---

# ЧАСТЬ A. БЭКЕНД (Linux)

## A1. Установка
Полностью по [DEPLOY.md](DEPLOY.md) (шаги 0–8): 4 сервиса, каждый в своём venv, `uv`/`pip`,
RAG-индекс через `ingest`, `.env`, запуск `HF_HUB_OFFLINE=1 bash run_api.sh`, приёмка
`bash scripts/healthcheck.sh --full`. Здесь — только пилотные акценты.

**Грабли, которые кусают именно на Linux/офлайн (все уже разобраны в DEPLOY.md):**
- **venv НЕ копируем с Mac** — пересоздаём на месте (`.venv`, `rag/.venv`, `.venv-spark`).
- **`rag/` venv нельзя сливать с основным** — `lightrag`/`torch` конфликтуют.
- **tiktoken-словарь** — папка `rag/vendor/tiktoken/` обязана приехать, иначе RAG офлайн не стартует.
- **Whisper-kk** (~1.6 ГБ) качается при первом kk-запросе — на офлайн-сервере нужен HF-кэш.
- **GPU**: `WHISPER_DEVICE=cuda`, в `spark_server/run.sh` — `SPARK_DEVICE="cuda"`.

## A2. Ключевые настройки `.env` (корень проекта)
Полный список — DEPLOY.md шаг 6. Обязательные для пилота:
```
LLM_BASE_URL=http://192.168.165.2:8901/v1
STT_URL=http://192.168.165.2:8804/transcribe     # ru STT (АФМ; kk — локальный Whisper)
STT_KK_USE_WHISPER=true
WHISPER_DEVICE=cuda
RAG_URL=http://localhost:8077/ask
TTS_PROVIDER=f5
F5_URL=http://192.168.165.2:8991/tts             # ru TTS с GPU-сервера АФМ (~1.5 c)
F5_REF_AUDIO=refs/ref_ru_f5.wav
F5_REF_TEXT=@refs/ref_ru.txt
TTS_KK_PROVIDER=spark
SPARK_URL=http://localhost:8809/tts
# --- ФРОНТЕНД/АВАТАР (Path B) ---
RENDER_PC_HOST=192.168.22.101                    # Windows-ПК с Unreal (player.js + сигналинг :80)
# LAST_ANSWER_TOKEN=                             # ПУСТО на пилоте! (см. A3)
```

## A3. ⚠️ `LAST_ANSWER_TOKEN` держим ПУСТЫМ на пилоте
Если задать токен — все `/last_answer*` требуют заголовок `X-Aidos-Token`. Браузер киоска
опрашивает `/last_answer/id` и качает WAV **без токена**, поэтому с токеном предзагрузка
звука отвалится (401). Аватар-клиент (Unreal) токен слать умеет, а браузер — нет.
Вывод: на пилоте `LAST_ANSWER_TOKEN` **не задаём**. (`/answer/<id>.wav` токеном не закрыт —
воспроизведение по событию выживет и с токеном, но пропадёт предзагрузка.)

## A4. Порт 8000 наружу
Оркестратор слушает `0.0.0.0:8000`. Проверь firewall Linux, чтобы киоск и render-ПК
достучались: `curl http://<LINUX-IP>:8000/health`. Приёмка по сети: `HOST=<LINUX-IP>
bash scripts/healthcheck.sh`.

## A5. 24/7
Боевой режим — systemd + сторож `/health`: [deploy/README.md](deploy/README.md)
(автозапуск, рестарт при падении/зависании). `run_api.sh` — для отладки.

---

# ЧАСТЬ B. ФРОНТЕНД + АВАТАР (Pixel Streaming, «Path B»)

**Идея Path B:** плеер Pixel Streaming отдаёт **сам бэкенд** (маршрут `/player`, тот же
origin, что `/avatar`). Раньше `/avatar` встраивала iframe прямо на render-ПК (кросс-origin)
и внедрить туда проигрывание звука было нельзя. Теперь движковый `player.js` грузится
кросс-origin с render-ПК, а наш `aidos-audio.js` — с того же origin, что и страница, и без
CORS ловит событие речи и играет WAV.

## B1. Как это собрано (файлы)
| Что | Где | Роль |
|-----|-----|------|
| `/avatar` | `app/static/avatar.html` | киоск-страница: iframe с плеером + кнопка/микрофон, панель ответа |
| `/player` | `app/main.py` (`_PLAYER_HTML`) | наш плеер: `player.js` (с render-ПК) + `aidos-audio.js` (свой) |
| `aidos-audio.js` | `app/static/aidos-audio.js` | ловит `aidos_speak`, качает WAV, играет со сдвигом |
| `RENDER_PC_HOST` | `app/config.py` / `.env` | IP render-ПК — **в одном месте**, питает и `player.js`, и `ss` |
| `/last_answer*`, `/answer/<id>.wav` | `app/main.py` | WAV ответа (+`X-Id`), CORS открыт |

## B2. Поток звука к зрителю (пошагово)
1. Гражданин жмёт «Задать вопрос» → микрофон → `POST /voice` (STT→RAG→TTS). Бэкенд
   кэширует WAV последнего ответа с новым `id` (`X-Id`).
2. `aidos-audio.js` в плеере опрашивает `GET /last_answer/id` (раз в `POLL_MS`) и, увидев
   новый `id`, **заранее** качает `GET /answer/<id>.wav` (blob), готовит к мгновенному старту.
3. Unreal прямо перед началом речи шлёт по data-каналу Pixel Streaming
   `{"type":"aidos_speak","id":"<id>"}`.
4. `aidos-audio.js` по событию играет подготовленный WAV **со сдвигом `SYNC_OFFSET_MS`**
   (подгоняем под губы). Всё same-origin → без CORS.

## B3. Render-ПК (Windows, Unreal)
- Встроенный сигналинг Pixel Streaming: HTTP-фронтенд и сигналинг игроков на **порту 80**,
  id стримера — **Editor**. `http://<RENDER_PC>/player.js` должен отдаваться (движковый фронтенд).
- Unreal должен слать в data-канал строку `{"type":"aidos_speak","id":"<ID_ОТВЕТА>"}`
  ровно перед началом артикуляции. `<ID_ОТВЕТА>` = `X-Id` из `/last_answer`. Контракт и
  как аватар берёт WAV — в [AVATAR.md](AVATAR.md).
- `aidos-audio.js` сам привяжется к `window.pixelStreaming`. Если объект называется иначе —
  вызвать `window.aidosAttachPixelStreaming(<ваш объект>)`. Проверка в консоли плеера:
  `[aidos] attached to PixelStreaming (response listener 'aidos')`.

## B4. Смена IP render-ПК — правим ОДНО место
IP живёт только в `RENDER_PC_HOST` (`.env`) → `settings.render_pc_host`. `/player` берёт из
него и `player.js`, и дефолтный `ss` (если iframe не передал — делает 307-редирект, добавляя
`ss=ws://<RENDER_PC_HOST>:80`). В `avatar.html` IP **нет**. Сменилась нода — правим `.env`,
рестарт бэкенда.

## B5. Автозвук (autoplay) — плашки нет
Браузер не даёт играть звук без жеста. Раньше была плашка «нажмите, чтобы включить звук» —
её убрали. Теперь `avatar.html` (тот же origin, что `/player`) разблокирует звук в плеере
через `iframe.contentWindow.aidosUnlock()`:
- в самом начале обработчика кнопки «Задать вопрос» (надёжно — по нему всё равно кликают);
- и на первый `pointerdown`/`keydown` (запасной путь).

`unlockAudio()` идемпотентна и «благословляет» и `AudioContext`, и `HTMLAudio`. Внутри
плеера остались `pointerdown`/`keydown`-слушатели как запас (но у iframe `pointer-events:none`
в киоске, так что основной путь — вызов из родителя).

## B6. Настройки в `aidos-audio.js` (вверху файла)
```js
const BACKEND = "";            // пусто = same-origin (Path B). НЕ трогать при переезде.
const SYNC_OFFSET_MS = 800;    // + = задержать звук (мс). ГЛАВНЫЙ тюнинг под губы, 100–800.
const POLL_MS = 1000;          // период предзагрузки нового ответа
const SHOW_UNLOCK_OVERLAY = false;  // видимая плашка выключена (разблокирует родитель)
```
- **`SYNC_OFFSET_MS`** — единственный параметр, который крутим вживую на пилоте: если звук
  опережает губы — увеличить, если отстаёт — уменьшить. Правится в одном файле, hard-refresh.
- **`BACKEND=""`** — фетчи идут на тот же origin, что и `/player` (его отдаёт бэкенд). При
  переезде на другой IP менять НЕ нужно. Абсолютный URL — только если плеер отдаётся с
  другого хоста, чем бэкенд (тогда полагаемся на открытый CORS `/last_answer`,`/answer`).

## B7. Открыть киоск
```
http://<LINUX-IP>:8000/avatar?kiosk=1
```
- `?kiosk=1` — режим тонкого клиента (крупные кнопки, бейдж языка, видео сверху).
- `?ar=9/16` (или `1/1`, `16/9`) — пропорция области видео под аспект рендера UE (без чёрных полос).
- Первый тап/клик по странице (или «Задать вопрос») включает звук. Управление камерой из
  видео заблокировано (`pointer-events:none`) — вопрос задаётся только кнопкой микрофона.

---

## C. Быстрая проверка пилота (чек-лист)
1. `curl http://<LINUX-IP>:8000/health` → `status:ok`, `rag.reachable:true`, `tts.enabled:true`.
2. `bash scripts/healthcheck.sh --full` (на сервере) → все ✔, сквозной STT→RAG→TTS.
3. `curl http://<LINUX-IP>:8000/player` → отдаёт HTML с `player.js` и `aidos-audio.js`.
4. На render-ПК запущен Unreal + Pixel Streaming (сигналинг :80, стример Editor);
   `http://<RENDER_PC>/player.js` открывается.
5. Открыть `/avatar?kiosk=1` на киоске → видит видео аватара, тапнуть для звука.
6. Консоль плеера (F12 в iframe): `[aidos] attached…`, при вопросе — `[aidos] speak event`,
   `[aidos] playing id=…`. Подогнать `SYNC_OFFSET_MS` под губы.
7. `LAST_ANSWER_TOKEN` пуст (иначе предзагрузка звука = 401).

## D. Если что-то не так
| Симптом | Причина / лечение |
|---------|-------------------|
| Видео есть, звука нет | Не было жеста → тапни / нажми «Задать вопрос». Консоль: `audio unlocked`? |
| Звук не в такт губам | Крути `SYNC_OFFSET_MS` в `aidos-audio.js`, hard-refresh (Ctrl+F5) |
| Звук играет, но с задержкой предзагрузки | Проверь `LAST_ANSWER_TOKEN` пуст; `/last_answer/id` отвечает |
| `[aidos] attached…` не появляется | Объект PS не `window.pixelStreaming` → `window.aidosAttachPixelStreaming(ps)` |
| Плеер чёрный | `http://<RENDER_PC>/player.js` не отдаётся, либо `RENDER_PC_HOST` неверный, либо сигналинг не на :80 |
| `/voice` падает | STT/RAG/TTS — смотри `scripts/healthcheck.sh`, доступность 192.168.165.2 |
| RAG не стартует офлайн | tiktoken-словарь `rag/vendor/tiktoken/` (DEPLOY.md шаг 3) |

Контакты сервисов АФМ (LLM/эмбеддинги/STT/ru-TTS) — в DEPLOY.md; аватар-контракт — AVATAR.md.
