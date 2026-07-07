"""Spark-TTS-Kazakh — сервер казахского TTS.

Запускается ОТДЕЛЬНО (venv .venv-spark: torch 2.8 + transformers 4.46), потому что
версии несовместимы с основным API. Основной API зовёт сюда по HTTP.

Контракт (его ждёт app/clients/tts.py -> _spark):
    POST /tts   {"text": "...", "language": "kazakh"}   ->  audio/wav
    GET  /health

Настройки (env):
    SPARK_REPO        путь к клону Spark-TTS (для импорта cli/sparktts)
    SPARK_MODEL_DIR   папка модели (models/spark-kazakh)
    SPARK_SPEAKER_WAV образец голоса для КЛОНИРОВАНИЯ (если задан — приоритетнее)
    SPARK_GENDER      male | female (режим без референса). default: male
    SPARK_PITCH       very_low|low|moderate|high|very_high. default: moderate
    SPARK_SPEED       very_low|low|moderate|high|very_high. default: moderate
    SPARK_DEVICE      cpu | cuda. default: cpu
    SPARK_PORT        default: 8809
"""
import io
import os
import sys
import threading

import soundfile as sf
import torch
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field

REPO = os.environ.get("SPARK_REPO", "spark_tts_repo")
MODEL_DIR = os.environ.get("SPARK_MODEL_DIR", "models/spark-kazakh")
SPEAKER_WAV = os.environ.get("SPARK_SPEAKER_WAV", "")
SPEAKER_TEXT = os.environ.get("SPARK_SPEAKER_TEXT", "")  # транскрипт образца — точнее клон
GENDER = os.environ.get("SPARK_GENDER", "male")
PITCH = os.environ.get("SPARK_PITCH", "moderate")
SPEED = os.environ.get("SPARK_SPEED", "moderate")
DEVICE = os.environ.get("SPARK_DEVICE", "cpu")
PORT = int(os.environ.get("SPARK_PORT", "8809"))
# По умолчанию слушаем ТОЛЬКО localhost: единственный клиент — оркестратор на той
# же машине. Для отдельной GPU-ноды поставь SPARK_HOST=0.0.0.0.
HOST = os.environ.get("SPARK_HOST", "127.0.0.1")
# Предел длины текста на один запрос: защита от DoS (долгий синтез на гигантском
# тексте + тихая обрезка модели на ~3000 токенов). Оркестратор шлёт куски <= ~180.
MAX_CHARS = int(os.environ.get("SPARK_MAX_CHARS", "3000"))
# Сколько раз перегенерировать при вырожденной выдаче (0 семантических токенов).
# Генерация стохастична (do_sample=True), поэтому повтор обычно помогает.
RETRIES = int(os.environ.get("SPARK_RETRIES", "2"))

sys.path.insert(0, REPO)  # чтобы импортировались cli.SparkTTS и sparktts
from cli.SparkTTS import SparkTTS  # noqa: E402

print(f"[spark] загружаю Spark-TTS-Kazakh из {MODEL_DIR} на {DEVICE}...")
_model = SparkTTS(MODEL_DIR, torch.device(DEVICE))
print("[spark] готово.")

# Один экземпляр модели на процесс; синхронный эндпоинт исполняется в пуле потоков.
# Параллельные model.generate непредсказуемо расходуют память (OOM на GPU) —
# сериализуем инференс блокировкой.
_infer_lock = threading.Lock()

app = FastAPI(title="Spark-TTS-Kazakh")


class TTSRequest(BaseModel):
    text: str = Field(..., max_length=MAX_CHARS)
    language: str | None = "kazakh"


@app.get("/health")
def health():
    return {"status": "ok", "gender": GENDER, "cloning": bool(SPEAKER_WAV)}


def _infer(text: str):
    """Один прогон синтеза с учётом режима (клонирование или gender)."""
    if SPEAKER_WAV:
        kw = {"prompt_speech_path": SPEAKER_WAV}
        if SPEAKER_TEXT:
            kw["prompt_text"] = SPEAKER_TEXT  # текст образца -> точнее клон
        return _model.inference(text, **kw)  # клонирование
    return _model.inference(text, gender=GENDER, pitch=PITCH, speed=SPEED)


@app.post("/tts")
def synthesize(req: TTSRequest):
    text = req.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="Пустой текст")
    # Модель иногда выдаёт 0 семантических токенов -> detokenize падает (einx: n=0).
    # Генерация стохастична, поэтому пробуем повторно, а не отдаём 500.
    last_err: Exception | None = None
    for attempt in range(RETRIES + 1):
        try:
            with _infer_lock:
                wav = _infer(text)
        except torch.cuda.OutOfMemoryError as e:
            # OOM повтором не лечится (только усугубляет) — отдаём сразу, без ретраев.
            torch.cuda.empty_cache()
            print(f"[spark] CUDA OOM: {e!r}")
            raise HTTPException(status_code=503, detail="TTS временно перегружен (нехватка памяти).")
        except Exception as e:  # вырожденная генерация и пр. сбои синтеза
            last_err = e
            print(f"[spark] попытка {attempt + 1}/{RETRIES + 1} не удалась: {e!r}")
            continue
        if wav is None or len(wav) == 0:  # пустой звук — тоже перегенерируем
            last_err = RuntimeError("синтез вернул пустой звук")
            print(f"[spark] попытка {attempt + 1}/{RETRIES + 1}: пустой звук")
            continue
        buf = io.BytesIO()
        sf.write(buf, wav, _model.sample_rate, format="WAV", subtype="PCM_16")
        return Response(content=buf.getvalue(), media_type="audio/wav")
    # Наружу — обобщённое сообщение (сырой текст исключения может содержать пути
    # модели/детали окружения); подробности уже в логах сервиса выше.
    print(f"[spark] исчерпаны попытки ({RETRIES + 1}); последняя ошибка: {last_err!r}")
    raise HTTPException(
        status_code=422,
        detail="Spark не смог озвучить текст (вырожденная генерация).",
    )


if __name__ == "__main__":
    uvicorn.run(app, host=HOST, port=PORT)
