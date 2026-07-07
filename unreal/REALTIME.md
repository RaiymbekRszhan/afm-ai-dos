# v4: реалтайм-аватар — «нажал Play и говоришь»

Цель: уйти от editor-пайплайна «WAV → MetaHuman Performance → Level Sequence →
Play» (офлайн-обработка, работает только вне Play-режима, копит ассеты) к живому
липсинку: **лицо аватара гонится от звука в реальном времени**, ответ просто
проигрывается как звук — без генерации ассетов вообще.

## Почему это возможно (проверено по probe от 2026-07-07)

В UE 5.7.4 ноды установлен плагин **MetaHumanLiveLink** (см. `debug/ue_log.txt`):
классы `MetaHumanLiveLinkAudioDevice`, `MetaHumanAudioLiveLinkSubjectSettings`,
`AudioDrivenAnimationModels/Mood/OutputControls`, `MetaHumanRealtimeSmoothingParams`,
Hyprsense-солвер, NNERuntimeORT. Это официальная фича Epic (UE 5.6+):
**MetaHuman (Audio) Live Link Source** — реалтайм-анимация лица от аудио-устройства,
работает и в редакторе, и в Play, и в упакованной игре (солвер на GPU через NNE).

Ограничение: источник слушает **аудио-устройство** (капчур-девайс), не SoundWave
напрямую. Стандартный обход — виртуальный аудиокабель (VB-Audio Virtual Cable):
звук ответа играем в «CABLE Input», Live Link слушает «CABLE Output».

## Архитектура v4

```
браузер/киоск (/avatar) → бэкенд /voice → WAV ответа
                                            │
нода: игровая логика (Blueprint) ← long-poll /last_answer/wait
  └ GET /last_answer → WAV → Runtime Audio Importer → AudioComponent.Play()
        звук → (Windows default = CABLE Input) → Live Link «MetaHuman (Audio)»
        → лицо в реальном времени; Pixel Streaming несёт видео+звук в браузер
```

Что это даёт против v3:
- **Play-режим и упаковка в .exe** — нода становится обычным приложением →
  запуск как Windows-сервис (NSSM), `-RenderOffscreen`, автостарт стрима флагом
  `-PixelStreamingURL` — 24/7 сильно надёжнее editor+watchdog;
- нет генерации ассетов на каждый ответ (нет утечки секвенций, ночной рестарт
  становится необязательным);
- ответ начинает звучать мгновенно (сейчас +~3-5 с на Performance/экспорт);
- idle-анимации/моргание/взгляд — обычный AnimBP поверх Live Link.

## Этап 1 — проверить реалтайм-липсинк руками (~1 час, без кода)

Ключевой риск — качество липсинка и потянет ли RTX 3050 солвер+рендер+кодирование
одновременно. Проверяется без единой строчки кода:

1. Поставить [VB-Audio Virtual Cable](https://vb-audio.com/Cable/) (скачать ЗАРАНЕЕ,
   в сети АФМ интернета нет). Windows Sound → устройство вывода по умолчанию =
   «CABLE Input».
2. UE: Window → Virtual Production → **Live Link** → Add Source →
   **MetaHuman (Audio)** → Audio Device = «CABLE Output» → Connect.
3. Настроить аватара на приём Live Link-субъекта (док Epic «Using a MetaHuman
   Audio Source»; у MetaHuman-BP есть стандартный Live Link-вход лица).
4. Проиграть любой WAV ответа обычным плеером Windows → лицо должно ожить
   **прямо в редакторе, без Play**.
5. Оценить: качество губ (рус/каз!), задержку, FPS, загрузку GPU.
6. Сохранить Live Link-пресет (Project Settings → Live Link → Default Preset) —
   он автоприменится при старте, в т.ч. в Play/packaged.

Если качество ок → этап 2. Если нет — остаёмся на v3 (Performance даёт
максимальное качество, т.к. решает офлайн).

## Этап 2 — игровая логика (Play-режим)

Editor-Python в Play не работает — логика `watch()` переезжает в Blueprint/C++
уровня (~4 узла):

1. **HTTP**: включить встроенный плагин «HTTP Blueprint» (или 30 строк C++):
   BeginPlay → цикл long-poll `GET /last_answer/wait?since=...` → при новом id
   `GET /last_answer` → байты WAV.
2. **Звук**: плагин **Runtime Audio Importer** (бесплатный, исходники на GitHub —
   скачать заранее, офлайн-совместим): байты WAV → SoundWave → AudioComponent.Play.
   Вывод — на системное устройство (= CABLE Input), Live Link подхватывает.
3. Idle-поза/моргание — AnimBP, Live Link поверх.
4. Проверка: **нажать Play → задать вопрос с /avatar → аватар отвечает сам.**

Микрофон гражданина остаётся в браузере (/avatar) — киоск не меняется.
(Опционально позже: мик прямо в игре через AudioCaptureComponent → POST /voice.)

## Этап 3 — упаковка и сервис (снимает editor-костыли)

Package → Windows .exe → запуск: `AidosAvatar.exe -RenderOffscreen
-PixelStreamingURL=ws://127.0.0.1:8888 -AudioMixer` под NSSM (сервис с
автоперезапуском). watchdog.ps1 упрощается до «процесс жив + heartbeat».

## Открытые вопросы (порядок выяснения)

1. `a.probe_realtime2()` на ноде → прислать вывод через /debug/log: точный API
   настроек субъекта (mood/smoothing/модели) и можно ли задать устройство
   программно (форумы: смена устройства в рантайме не открыта — нам ок,
   устройство фиксированное, пресет решает).
2. Тянет ли 3050 солвер в реалтайме при 30+ FPS рендера (этап 1, п.5).
3. Ведёт ли себя солвер адекватно на казахской речи.
4. Pixel Streaming в packaged: пересобрать поток видео+звук, проверить задержку.

Ссылки: [Audio Driven Animation](https://dev.epicgames.com/documentation/metahuman/audio-driven-animation) ·
[Realtime Animation](https://dev.epicgames.com/documentation/metahuman/realtime-animation) ·
[Using a MetaHuman Audio Source](https://dev.epicgames.com/documentation/en-us/metahuman/using-a-metahuman-audio-source)

---

## Для сессии Claude Code НА НОДЕ (Windows, план от 2026-07-07)

Контекст: ты работаешь на Windows-ПК с Unreal 5.7.4 и MetaHuman-аватаром
(проект с актором `BP_AFM_agent_v03`). Бэкенд (оркестратор :8000) — на Маке
разработчика в той же сети; адрес спросить у пользователя (DHCP) и проверить
`curl <BACKEND>/health`. Текущий рабочий пайплайн v3 — editor-скрипт
`aidos_editor.py` (см. README.md этой папки, там же все известные грабли UE).

Порядок работ (согласован):

1. **Осмотреться**: путь проекта UE, `Saved/Logs/` (свежий лог редактора),
   версии плагинов; `curl` до бэкенда. Включён ли Python Remote Execution
   (Project Settings → Plugins → Python) — если да, выполнять Python в живом
   редакторе можно через upyrc/remote exec, если нет — попросить включить.
2. **Инфраструктура 24/7 (v3)** — поставить и проверить: `watchdog.ps1`
   (шапку — под реальные пути), `schtasks` ONLOGON, автологон (реестр:
   `HKLM\...\Winlogon\AutoAdminLogon`), `init_unreal.py` в `Content/Python/`,
   выключить сон/автообновления. Приёмка: убить UnrealEditor → сам поднялся →
   `/health` бэкенда показывает `node.watching: true`.
3. **Этап 1 v4 (реалтайм-липсинк)** — по плану выше: VB-Cable (установщик —
   у пользователя, нужен UAC), Live Link source «MetaHuman (Audio)»,
   `a.probe_realtime2()` для API. Приёмка: WAV играется плеером → лицо живёт
   в редакторе; спросить пользователя про качество губ (ru и kk!) и FPS.
4. **Дальше по результату**: этап 2 (игровая логика C++/Blueprint) — только
   если этап 1 показал приемлемое качество.

Грабли, о которых знает Mac-сессия: editor-скрипты не работают в Play-режиме;
`delete_asset` на Level Sequence вешает модалку (секвенции не удалять);
`-noepicportal` и выключенный AutoSave — от фризов; звук ноды — в ноль
(эхо/рассинхрон с Pixel Streaming). Скриншот экрана для самопроверки:
PowerShell `System.Windows.Forms` + `Graphics.CopyFromScreen`, потом Read.
