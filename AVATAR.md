# AVATAR.md — контекст для работы над аватаром на Windows-ПК

Этот файл — вся память проекта по аватару. Он написан для сессии Claude Code,
запущенной на Windows-ПК с Unreal Engine (и для любого человека, который
подхватит работу). Бэкенд живёт в этом же репозитории и **уже работает** —
его не трогаем, аватар только потребляет его API.

## Миссия

Голосовой «цифровой офицер» **Ai-dos** для Агентства РК по финансовому
мониторингу (АФМ). Гражданин задаёт вопрос голосом — MetaHuman-аватар отвечает
голосом, строго по базе кодексов и законов РК. Цель работы на этом ПК:
**всё время живой аватар, с которым можно разговаривать** — работает 24/7,
отвечает сам, без человека за пультом.

Итоговая цель по шагам:
1. Аватар в Play-режиме (или упакованный .exe) сам забирает готовые озвученные
   ответы с бэкенда и проговаривает их с липсинком в реальном времени.
2. Работает непрерывно: автозапуск, перезапуск при падении, наблюдаемость.
3. Видео аватара доступно гражданину (Pixel Streaming в браузер или экран киоска).

## Железо и софт на этом ПК

- Windows, ноутбук с **RTX 3050** (солвер липсинка, рендер и кодирование видео
  делят один GPU — следить за FPS).
- **Unreal Engine 5.7.4** + плагины: MetaHuman (Animator), **MetaHumanLiveLink**
  (проверено — установлен), PixelStreaming2, PythonScriptPlugin, NNERuntimeORT.
- Проект UE с MetaHuman-актором **`BP_AFM_agent_v03`**
  (ассет `/Game/MetaHumans/AFM_agent_v02/BP_AFM_agent_v03`).
- Финальная сеть АФМ — **без интернета**: всё (плагины, инсталляторы) скачивать,
  пока интернет есть. Claude Code в сети АФМ работать не будет — вся разработка
  сейчас, дома.

## Контракт с бэкендом (ничего другого аватару не нужно)

Бэкенд (FastAPI, порт 8000) — на Маке разработчика в той же сети. IP выдаётся
DHCP — **спросить у пользователя** и проверить `curl http://<IP>:8000/health`.

| Эндпоинт | Что делает |
|---|---|
| `GET /health` | статус; поле `node.watching` — жив ли аватар-клиент (см. heartbeat) |
| `POST /voice` | multipart `data`=WAV + `language`=russian\|kazakh → **WAV ответа** (заголовки `X-Question`/`X-Answer` — percent-encoded текст) |
| `GET /last_answer` | последний озвученный ответ: тело = WAV, заголовки `X-Question`, `X-Answer`, `X-Id` |
| `GET /last_answer/id` | `{"id": "..."}` — лёгкая проверка «есть ли новый ответ» |
| `GET /last_answer/wait?since=<id>&timeout=25` | long-poll: висит до ответа новее `since` (timeout 1–55 с) → `{"id": ...}` |
| `GET /answer/<id>.wav` | конкретный озвученный ответ по id (WAV, CORS `*`, `X-Id`) — для зрительского фронта; держим последние 32 ответа |

- Гражданин спрашивает через браузер (страница `/` бэкенда) → `/voice` кэширует
  озвученный ответ → аватар узнаёт о нём через `/last_answer/wait` и забирает WAV.
- **Токен**: если на бэкенде задан `LAST_ANSWER_TOKEN`, все `/last_answer*` требуют
  заголовок `X-Aidos-Token` (иначе 401). По умолчанию выключен.
- **Heartbeat**: каждый запрос к `/last_answer*` отмечает аватара живым; тишина
  >90 с → `/health` показывает `node.watching:false`. Это сигнал для watchdog:
  «клиент завис — перезапусти». Цикл опроса держать ≤30 с (wait timeout=25).
- WAV: PCM, обычно 24 кГц моно (F5/Spark). Ответ длинный (до минуты речи).
- **Звук зрителю (Pixel Streaming), Path B**: голос играется на рендер-ПК и в
  WebRTC-поток не попадает — проигрываем WAV в браузере зрителя синхронно с губами.
  Бэкенд САМ отдаёт плеер PS: `GET /player` (тот же origin, что `/avatar`) отдаёт
  минимальный HTML с движковым `player.js` (кросс-origin с render-ПК) и своим
  `app/static/aidos-audio.js` (same-origin). `/avatar` встраивает iframe → `/player`
  (без `ss` — `/player` сам редиректит, добавляя `ss=ws://<render_pc_host>:80`).
  aidos-audio.js привязывается к `window.pixelStreaming`, по data-каналу UE перед
  речью шлёт `{"type":"aidos_speak","id":"<id>"}`, скрипт по `X-Id` заранее качает
  WAV (`/answer/<id>.wav`, фолбэк `/last_answer`) и играет со сдвигом `SYNC_OFFSET_MS`
  (тюнинг ~100–400 мс). IP render-ПК — одна настройка `RENDER_PC_HOST` (app/config.py,
  дефолт `192.168.22.101`), правит и `player.js`, и `ss`. CORS на `/last_answer` и
  `/answer/<id>.wav` оставлен открытым (`*`) — на случай доступа к бэкенду с иного origin.
- Один слот ответа: два одновременных вопроса перетирают друг друга — для пилота
  с одним киоском это принято как ограничение.

## Что уже было сделано и РАБОТАЛО (v3, editor-пайплайн) — опыт сохранён

Весь код v3 — в истории git этого репо, коммит **`60b2854`** (папка `unreal/`):

```bash
git show 60b2854:unreal/aidos_editor.py > aidos_editor.py   # ~950 строк, рабочий
git show 60b2854:unreal/README.md                            # инструкции + грабли
git show 60b2854:unreal/REALTIME.md                          # план v4 (реалтайм)
git show 60b2854:unreal/watchdog.ps1                          # сторож для 24/7
git show 60b2854:unreal/init_unreal.py                        # автозапуск при старте UE
```

v3 работал так (в РЕДАКТОРЕ, не в Play): `a.watch()` в Python-консоли UE →
long-poll бэкенда в фоновом потоке → WAV → импорт SoundWave → **MetaHuman
Performance** (Input=Audio, офлайн-солв ~3 с на 3050) → экспорт Level Sequence →
авто-Play в редакторе. Полный цикл подтверждён вживую 2026-07-07. Недостатки:
работает только в редакторе (не Play/exe), на каждый ответ создаёт ассеты
(копятся — см. грабли про delete_asset), +3–5 с задержки на солв.

### Грабли UE 5.7, найденные кровью (НЕ повторять)

1. **`delete_asset` на Level Sequence = блокирующая модалка «asset in use» +
   порча пакета.** Секвенции не удалять программно; чистить только WAV/Performance.
2. **Editor-скрипты не работают в Play-режиме** (в v4 логика должна жить в
   Blueprint/C++ уровня, тогда Play — штатный режим).
3. Экспорт секвенции: `target_meta_human_class` ждёт **Blueprint-ассет** (не
   класс); `export_transform_track=False`, иначе запекается кривая позиция.
4. Импорт ассета делать ДО чистки старых, deferred play — через несколько
   «спокойных» кадров (register_slate_post_tick_callback).
5. Запуск редактора: `-noepicportal`, AutoSave выключить — иначе фризы.
6. Имена биндингов в секвенции — с пробелами («BP AFM Agent V 03»), сравнивать
   нормализованно.
7. Звук Windows-ПК в ноль при Pixel Streaming (иначе эхо и рассинхрон) — но для
   v4 с VB-Cable схема звука другая, см. ниже.

## План v4 — реалтайм-липсинк (то, что делаем на этом ПК)

Разведка `probe_realtime()` (запущена на этом ПК 2026-07-07; сама функция — в
`aidos_editor.py` из истории, там же `probe_realtime2()`) подтвердила: в UE
5.7.4 установлен **MetaHumanLiveLink** — официальный реалтайм audio-to-face
(Epic, UE 5.6+): классы `MetaHumanLiveLinkAudioDevice`,
`MetaHumanAudioLiveLinkSubjectSettings`, `AudioDrivenAnimationModels/Mood/
OutputControls`, `MetaHumanRealtimeSmoothingParams`; солвер локальный (NNE/ONNX,
офлайн-совместим). Работает в редакторе, в Play и в packaged build.

**Ограничение**: источник «MetaHuman (Audio)» слушает **аудио-устройство**
(вход), не SoundWave. Обход — виртуальный аудиокабель **VB-Audio Virtual Cable**:
звук ответа играется в «CABLE Input», Live Link слушает «CABLE Output».
Смена устройства в рантайме в API не открыта — устройство фиксируем через
Live Link **пресет** (Project Settings → Live Link → Default Live Link Preset —
автоприменяется при старте, в т.ч. в Play/packaged).

### Этап 1 — proof реалтайм-липсинка (~1 час, без кода)
1. Установить VB-Cable (UAC — попросить пользователя). Вывод Windows по
   умолчанию = «CABLE Input».
2. UE: Window → Virtual Production → Live Link → Add Source →
   **MetaHuman (Audio)** → устройство «CABLE Output» → Connect.
3. Подключить субъект к лицу `BP_AFM_agent_v03` (док Epic «Using a MetaHuman
   Audio Source»; у MetaHuman-BP есть штатный Live Link-вход).
4. Проиграть WAV ответа любым плеером → **лицо оживает прямо в редакторе**.
5. Оценить с пользователем: качество губ на РУССКОМ и КАЗАХСКОМ, задержку, FPS.
6. Сохранить Live Link-пресет как Default.

Если качество плохое — fallback: остаёмся на v3 (код в истории, он рабочий),
качество Performance выше (офлайн-солв).

### Этап 2 — «нажал Play и говоришь»
Логика в Blueprint или C++ (Python в Play не работает):
- BeginPlay → цикл: `GET /last_answer/wait?since=...` (встроенный плагин
  «HTTP Blueprint» или C++ FHttpModule) → при новом id `GET /last_answer` → байты.
- WAV → звук: плагин **Runtime Audio Importer** (бесплатный, скачать заранее)
  → AudioComponent.Play (выход на системное устройство = CABLE Input → Live Link
  подхватывает, лицо живёт).
- Idle-анимации/моргание — обычный AnimBP, Live Link поверх.
- Приёмка: Play → вопрос голосом со страницы `/` бэкенда → аватар ответил сам.

### Этап 3 — 24/7 сервис
- Packaged .exe (`-RenderOffscreen -PixelStreamingURL=ws://127.0.0.1:8888`) под
  NSSM/Task Scheduler, автологон, сон выключить.
- Watchdog: процесс жив + `GET /health` бэкенда → `node.last_poll_ago_sec` < 180,
  иначе перезапуск. Готовый шаблон: `git show 60b2854:unreal/watchdog.ps1`
  (он писался под перезапуск редактора — заменить пути на .exe).
- Pixel Streaming: signalling server (WebServers из PixelStreaming2, скачаны
  заранее через get_ps_servers.bat) слушает LAN (не 127.0.0.1), автозапуск.

## Как работать (заметки для Claude на ПК)

- Логи UE: `<проект>/Saved/Logs/*.log` — читать напрямую.
- Python в живом редакторе: включить Project Settings → Plugins → Python →
  **Remote Execution**, дальше можно выполнять команды снаружи; либо просить
  пользователя вставлять в консоль UE.
- Самопроверка картинки: скриншот через PowerShell
  (`System.Windows.Forms`/`CopyFromScreen`) и прочитать файл изображения.
- Я не слышу звук: качество голоса/липсинка оценивает пользователь.
- Микрофон гражданина остаётся в браузере (страница `/` бэкенда) — на ПК его
  делать не нужно (опция на потом: AudioCapture в игре → POST /voice).
- Известные держатели времени: прогрев бэкенда (Whisper), TTS ~1.5 с/кусок на
  GPU АФМ; ответ RAG на медленном LLM — десятки секунд. Терпеливые таймауты.

## Промт для первого запуска на ПК

> Прочитай AVATAR.md в корне репозитория — это весь контекст. Мы делаем этап 1
> (проверка реалтайм-липсинка MetaHuman Live Link). Бэкенд доступен по адресу
> http://<IP>:8000 (проверь /health). Осмотрись на машине (пути UE-проекта,
> логи), составь план и веди меня по шагам этапа 1; что можешь — делай сам.
