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

import soundfile as sf
import torch
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

REPO = os.environ.get("SPARK_REPO", "spark_tts_repo")
MODEL_DIR = os.environ.get("SPARK_MODEL_DIR", "models/spark-kazakh")
SPEAKER_WAV = os.environ.get("SPARK_SPEAKER_WAV", "")
SPEAKER_TEXT = os.environ.get("SPARK_SPEAKER_TEXT", "")  # транскрипт образца — точнее клон
GENDER = os.environ.get("SPARK_GENDER", "male")
PITCH = os.environ.get("SPARK_PITCH", "moderate")
SPEED = os.environ.get("SPARK_SPEED", "moderate")
DEVICE = os.environ.get("SPARK_DEVICE", "cpu")
PORT = int(os.environ.get("SPARK_PORT", "8809"))

sys.path.insert(0, REPO)  # чтобы импортировались cli.SparkTTS и sparktts
from cli.SparkTTS import SparkTTS  # noqa: E402

print(f"[spark] загружаю Spark-TTS-Kazakh из {MODEL_DIR} на {DEVICE}...")
_model = SparkTTS(MODEL_DIR, torch.device(DEVICE))
print("[spark] готово.")

app = FastAPI(title="Spark-TTS-Kazakh")


class TTSRequest(BaseModel):
    text: str
    language: str | None = "kazakh"


@app.get("/health")
def health():
    return {"status": "ok", "gender": GENDER, "cloning": bool(SPEAKER_WAV)}


@app.post("/tts")
def synthesize(req: TTSRequest):
    if not req.text.strip():
        raise HTTPException(status_code=400, detail="Пустой текст")
    if SPEAKER_WAV:
        kw = {"prompt_speech_path": SPEAKER_WAV}
        if SPEAKER_TEXT:
            kw["prompt_text"] = SPEAKER_TEXT  # текст образца -> точнее клон
        wav = _model.inference(req.text, **kw)  # клонирование
    else:
        wav = _model.inference(req.text, gender=GENDER, pitch=PITCH, speed=SPEED)
    buf = io.BytesIO()
    sf.write(buf, wav, _model.sample_rate, format="WAV", subtype="PCM_16")
    return Response(content=buf.getvalue(), media_type="audio/wav")


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)
