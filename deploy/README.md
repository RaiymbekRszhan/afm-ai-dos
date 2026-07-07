# Запуск 24/7 через systemd (сервер АФМ)

`run_api.sh` хорош для отладки, но для киоска нужен **автозапуск при загрузке** и
**перезапуск при падении**. Эти юниты делают именно это (вместо `run_api.sh`).

4 сервиса = 4 юнита. `ai-dos-api` зависит от остальных трёх (поднимутся раньше).
Пятый юнит — сторож `ai-dos-watchdog.timer`: `Restart=always` ловит только
падение процесса, а таймер раз в 2 минуты дёргает `/health` и перезапускает
`ai-dos-api`, если тот жив, но не отвечает 3 проверки подряд (зависший
GPU-вызов, дедлок).

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
sudo systemctl enable --now ai-dos-f5 ai-dos-spark ai-dos-rag ai-dos-api
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
- **Рендер-нода (Windows/Unreal) — отдельная история**: её 24/7 обеспечивают
  `unreal/init_unreal.py` + `unreal/watchdog.ps1` (см. `unreal/README.md`,
  раздел «Работа 24/7»). Жива ли нода, видно в `curl localhost:8000/health` →
  поле `node.watching`.
