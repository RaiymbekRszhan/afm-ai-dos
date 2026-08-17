# Запуск 24/7 через systemd (сервер АФМ)

`run_api.sh` хорош для отладки, но для киоска нужен **автозапуск при загрузке** и
**перезапуск при падении**. Эти юниты делают именно это (вместо `run_api.sh`).

Сервис = юнит. Пилотный минимум — три: `ai-dos-rag` (источник ответа),
`ai-dos-api` (оркестратор, зависит от rag) и `ai-dos-video-ui` (киоск-страница
:8100 — фронтенд, который видит гражданин; зависит от api). Локальные
`ai-dos-f5` / `ai-dos-omni` / `ai-dos-spark` / `ai-dos-whisper-kk` нужны, только если TTS/STT
крутятся на этой же машине, а не на сервере АФМ.
Отдельный юнит — сторож `ai-dos-watchdog.timer`: `Restart=always` ловит только
падение процесса, а таймер раз в 2 минуты дёргает `/health` и перезапускает
`ai-dos-api`, если тот жив, но не отвечает 3 проверки подряд (зависший
GPU-вызов, дедлок).

## Единый источник окружения (топология TTS/STT) — N2
Топология (какой TTS/STT, по каким адресам) задаётся в ОДНОМ файле — `.env` в корне
проекта. Его читает `app/config.py` (pydantic), поэтому он действует и под systemd
(юнит с `WorkingDirectory=/opt/ai-dos` → читается `/opt/ai-dos/.env`), и при запуске
без него. `run_api.sh` может переопределить значения для отладки (флаги `USE_LOCAL_*`),
но боевой 24/7-путь берёт топологию из `.env`. Так «деплой по инструкции» = то, что
проверяли — без расхождения «как в юните» ↔ «как в скрипте».

- Пример и все поля — в `.env.example` (скопируй в `.env` и подгони).
- Пилотная топология: русский F5 на GPU-ноде АФМ (`:8991`) + STT-сервер АФМ (`:8804`);
  казахский TTS — переключатель `TTS_KK_PROVIDER`: `omni` (OmniVoice на GPU-ноде
  АФМ, `OMNI_URL=…:8993` — основной путь), `eleven` (облако, страховка) или
  `spark` (прежний движок, `SPARK_URL=…:8992`). Локальных F5/OmniVoice/Spark/Whisper
  на этой машине нет — соответствующие юниты не нужны.
- ⚠️ НЕ добавляй в юнит строки `Environment=F5_URL=…` и т.п.: переменные окружения
  приоритетнее `.env` и молча перебьют его (ровно это и чинил N2).

## Перед установкой
Сначала пройди [../DEPLOY.md](../DEPLOY.md) (шаги 1–6): код скопирован, 4 venv созданы,
индекс собран (`ingest`), `.env` и `rag/.env` заполнены, всё хоть раз поднялось руками.

## Подгонка под сервер
В юнитах прописаны (поправь, если у тебя иначе):
- путь проекта **`/opt/ai-dos`** → замени на свой (напр. `/home/afm/STT`);
- пользователь **`ai-dos`** → замени на своего;
- **`cuda`** в `F5_DEVICE`/`OMNI_DEVICE`/`SPARK_DEVICE`/`WHISPER_DEVICE` → если сервер без GPU, убери эти строки.
  (F5 на CPU особенно медленный — для боя GPU обязателен.)

Быстрая замена пути и пользователя (пример):
```bash
cd deploy
sed -i 's#/opt/ai-dos#/home/afm/STT#g; s/User=ai-dos/User=afm/g' ai-dos-*.service
```

## Машинно-зависимые ключи запуска — `/etc/default/ai-dos`
Порт киоск-страницы, путь к кэшу tiktoken, `SSL_CERT_FILE` для TLS-прокси — всё,
что зависит от КОНКРЕТНОЙ машины, а не от проекта. Юниты читают этот файл
(`EnvironmentFile=-/etc/default/ai-dos`, минус = «нет файла — не беда»).

```bash
sudo cp deploy/ai-dos.env.example /etc/default/ai-dos
sudo nano /etc/default/ai-dos
```
Зачем отдельный файл: раньше это правили прямо в `run_api.sh` и `video_ui/run.sh`
на сервере, и каждое обновление кода стирало правки — страница уезжала с `:80`
на `:8100` (все киоски видели «недоступно»), а RAG не стартовал без верного пути
к словарю. ⚠️ Топологию TTS/STT и секреты сюда класть НЕЛЬЗЯ (см. N2 выше):
их место — `.env` в корне проекта.

## Установка
```bash
sudo cp deploy/ai-dos-*.service deploy/ai-dos-watchdog.timer /etc/systemd/system/
sudo systemctl daemon-reload
# Пилот: TTS/STT на сервере АФМ, локально нужны rag + оркестратор + киоск-страница:
sudo systemctl enable --now ai-dos-rag ai-dos-api ai-dos-video-ui
# ТОЛЬКО если гоняешь TTS/STT локально на ЭТОЙ машине (не на сервере АФМ):
# sudo systemctl enable --now ai-dos-f5 ai-dos-omni         # локальный TTS (ru + kk)
# sudo systemctl enable --now ai-dos-whisper-kk             # локальный казахский STT
sudo systemctl enable --now ai-dos-watchdog.timer     # сторож /health (раз в 2 мин)
```
В `ai-dos-watchdog.service` путь скрипта — `/opt/ai-dos/scripts/api_watchdog.sh`:
если проект лежит не в `/opt/ai-dos`, поправь (тот же sed, что выше).

## Проверка и управление
```bash
systemctl status ai-dos-api          # статус (и любой другой сервис)
curl http://localhost:8000/health    # status:ok, rag.reachable:true, tts.servers.*.reachable:true
journalctl -u ai-dos-api -f          # логи в реальном времени
journalctl -u ai-dos-rag -n 100      # последние 100 строк RAG

sudo systemctl restart ai-dos-api    # перезапуск
sudo systemctl stop ai-dos-f5        # остановить сервис
```

## Важно
- **Порядок старта** мягкий: оркестратор может подняться раньше, чем модели TTS прогрузятся
  (F5/OmniVoice/Spark/Whisper грузятся в память ~десятки секунд). Это нормально — до прогрузки
  `/voice` вернёт ошибку, после — заработает; `Restart=always` подстрахует от падений.
- **Первый старт дольше**: модели читаются в память (на GPU быстрее). Следи `journalctl`.
- **Офлайн**: `HF_HUB_OFFLINE=1` уже в юнитах — модели должны быть скачаны заранее (DEPLOY.md).
- **Обновил код/базу?** `sudo systemctl restart ai-dos-api` (и/или нужный сервис).
  После правки `rag/data/` — пересобери индекс (`ingest`) и `restart ai-dos-rag`.
- **Фронтенд пилота** — киоск-страница `video_ui/` (:8100). Под systemd это
  `ai-dos-video-ui.service` (в `run_api.sh` она стартует сама, `WITH_VIDEO_UI=1`).
  Своего venv не требует — идёт из основного `/opt/ai-dos/.venv`. Бэкенд задаётся
  в юните (`Environment=AIDOS_BACKEND=http://127.0.0.1:8000`) — это единственное
  исключение из правила «топология в `.env`»: `video_ui` не читает `.env` проекта.
  Проверка — `curl http://localhost:8100/health`: `backend_reachable:true` и
  `videos:{idle.mp4:true, talk.mp4:true}` (ролики ~21 МБ, при копировании через
  scp их легко забыть → чёрный экран на киоске).

## Сужение прав watchdog (перед боевой эксплуатацией)
`ai-dos-watchdog.service` сейчас работает от root (нужен `systemctl restart`).
Для пилота это не блокер, но для 24/7-эксплуатации лучше не держать root ради
одной команды. Вариант — запускать сторож от того же пользователя `ai-dos`, что
и остальные сервисы, и выдать ему через sudoers только этот один рестарт:

```bash
# /etc/sudoers.d/ai-dos-watchdog (0440, редактировать через visudo -f)
ai-dos ALL=(root) NOPASSWD: /usr/bin/systemctl restart ai-dos-api
```

Затем в `ai-dos-watchdog.service` добавить `User=ai-dos` и `[Service]` секцию,
а в `scripts/api_watchdog.sh` заменить `systemctl restart "$UNIT"` на
`sudo systemctl restart "$UNIT"`. `/run/ai-dos-watchdog.fails` (счётчик неудач)
при этом нужно перенести из `/run` (root-only на запись) в каталог, который
systemd создаст для пользователя `ai-dos` — добавь в юнит
`RuntimeDirectory=ai-dos-watchdog` и поменяй `STATE` в скрипте на
`/run/ai-dos-watchdog/fails`. Это стоит проверить руками на самом сервере
(`sudo -u ai-dos sudo systemctl restart ai-dos-api` должен пройти без пароля,
любая другая команда через `sudo -u ai-dos sudo ...` — нет) перед тем, как
полагаться на это в бою.
