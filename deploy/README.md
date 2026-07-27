# Запуск 24/7 через systemd (сервер АФМ)

`run_api.sh` хорош для отладки, но для киоска нужен **автозапуск при загрузке** и
**перезапуск при падении**. Эти юниты делают именно это (вместо `run_api.sh`).

4 сервиса = 4 юнита. `ai-dos-api` зависит от остальных трёх (поднимутся раньше).
Пятый юнит — сторож `ai-dos-watchdog.timer`: `Restart=always` ловит только
падение процесса, а таймер раз в 2 минуты дёргает `/health` и перезапускает
`ai-dos-api`, если тот жив, но не отвечает 3 проверки подряд (зависший
GPU-вызов, дедлок).

## Единый источник окружения (топология TTS/STT) — N2
Топология (какой TTS/STT, по каким адресам) задаётся в ОДНОМ файле, который читают
**оба** пути запуска — и systemd (`ai-dos-api.service` через `EnvironmentFile`), и
`run_api.sh` (сорсит его в начале). Так «деплой по инструкции» = то, что проверяли,
без расхождения «как в юните» ↔ «как в скрипте».

```bash
cp deploy/ai-dos.env.example ai-dos.env   # в корень проекта (в git не коммитим)
# отредактируй ai-dos.env: выбери топологию (A) GPU-F5 + ElevenLabs  или
#                                             (B) полностью локальный F5 + Spark
```
- **Секреты** (`ELEVENLABS_API_KEY`, `ELEVENLABS_VOICE_ID`) — в `.env`, НЕ в `ai-dos.env`
  (оба читает `app/config.py`).
- Топология **(A)** (пилотный дефолт): русский F5 на GPU-ноде АФМ + казахский
  ElevenLabs — локальные `ai-dos-f5`/`ai-dos-spark` НЕ нужны, их не `enable`.
- Топология **(B)** (офлайн): локальные F5 + Spark — тогда `enable --now ai-dos-f5
  ai-dos-spark` (см. «Установка» ниже).

## Перед установкой
Сначала пройди [../DEPLOY.md](../DEPLOY.md) (шаги 1–6): код скопирован, 4 venv созданы,
индекс собран (`ingest`), `.env` и `rag/.env` заполнены, всё хоть раз поднялось руками.

## Подгонка под сервер
В юнитах прописаны (поправь, если у тебя иначе):
- путь проекта **`/opt/ai-dos`** → замени на свой (напр. `/home/afm/STT`);
- пользователь **`ai-dos`** → замени на своего;
- **`cuda`** в `F5_DEVICE`/`SPARK_DEVICE`/`WHISPER_DEVICE` → если сервер без GPU, убери эти строки.
  (F5 на CPU особенно медленный — для боя GPU обязателен.)

Быстрая замена пути и пользователя (пример):
```bash
cd deploy
sed -i 's#/opt/ai-dos#/home/afm/STT#g; s/User=ai-dos/User=afm/g' ai-dos-*.service
```

## Установка
```bash
sudo cp deploy/ai-dos-*.service deploy/ai-dos-watchdog.timer /etc/systemd/system/
sudo systemctl daemon-reload
# Топология (A) GPU-F5 + ElevenLabs (пилотный дефолт) — локальные TTS не нужны:
sudo systemctl enable --now ai-dos-rag ai-dos-api
# Топология (B) локальный TTS — дополнительно подними F5 + Spark:
# sudo systemctl enable --now ai-dos-f5 ai-dos-spark
# Локальный казахский STT (STT_KK_USE_WHISPER=true) — ещё и whisper-kk:
# sudo systemctl enable --now ai-dos-whisper-kk
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
  (F5/Spark/Whisper грузятся в память ~десятки секунд). Это нормально — до прогрузки
  `/voice` вернёт ошибку, после — заработает; `Restart=always` подстрахует от падений.
- **Первый старт дольше**: модели читаются в память (на GPU быстрее). Следи `journalctl`.
- **Офлайн**: `HF_HUB_OFFLINE=1` уже в юнитах — модели должны быть скачаны заранее (DEPLOY.md).
- **Обновил код/базу?** `sudo systemctl restart ai-dos-api` (и/или нужный сервис).
  После правки `rag/data/` — пересобери индекс (`ingest`) и `restart ai-dos-rag`.
- Фронтенд пилота — киоск-страница `video_ui/` (:8100), поднимается вместе со
  стеком (`run_api.sh`) или отдельным юнитом при желании.

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
