"""
Whisper-kk STT-сервер (казахский) для GPU-сервера АФМ.

Выносит локальный in-process Whisper-kk (app/clients/stt.py) в отдельный HTTP-
сервис. Тогда казахский STT крутится на GPU АФМ, а киоск/оркестратор остаются
ЛЁГКИМИ — без torch/transformers/весов модели.

Контракт РОВНО тот же, что у STT-сервера АФМ (app/clients/stt.py -> _afm), поэтому
оркестратору достаточно указать на нас в STT_KK_URL — код менять не нужно:
    POST /transcribe   (multipart: data=<аудиофайл>, language=<str>)
        -> {"status": "success", "data": "<распознанный текст>"}
    GET  /health       -> {"status": "ok", ...}

Переменные окружения:
    WHISPER_KK_MODEL  путь/HF-id модели  (default: shyngys879/kazakh-whisper-large-v3-turbo)
    WHISPER_DEVICE    auto | cpu | cuda | mps  (default: auto; на GPU АФМ — cuda)
    WHISPER_KK_PORT   default: 8813
"""
import asyncio
import io
import os
import threading

import librosa
import soundfile as sf
import torch
import uvicorn
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from transformers import pipeline
from transformers.utils import logging as hf_logging

MODEL = os.environ.get("WHISPER_KK_MODEL", "shyngys879/kazakh-whisper-large-v3-turbo")
PORT = int(os.environ.get("WHISPER_KK_PORT", "8813"))

# Ленивый кэш пайплайна + double-checked locking: прогрев (startup) и первый
# запрос иначе оба увидят None и загрузят гигабайты модели ДВАЖДЫ (скачок/OOM).
_whisper = None
_lock = threading.Lock()


def _device() -> str:
    dev = os.environ.get("WHISPER_DEVICE", "auto")
    if dev != "auto":
        return dev
    if torch.cuda.is_available():
        return "cuda"
    if torch.backends.mps.is_available():
        return "mps"
    return "cpu"


def _load() -> None:
    global _whisper
    if _whisper is not None:
        return
    with _lock:
        if _whisper is not None:
            return
        # Глушим info/warning Whisper (logits processor, pad_token_id и т.п.) — шум.
        hf_logging.set_verbosity_error()
        dev = _device()
        _whisper = pipeline(
            "automatic-speech-recognition",
            model=MODEL,
            device=dev,
            # длинное аудио: иначе Whisper режет на 30 с и теряет хвост —
            # chunk_length_s включает скользящее окно с батчингом.
            chunk_length_s=30,
            batch_size=8,
            # на GPU быстрее и экономнее в float16, на CPU/MPS — float32
            dtype=torch.float16 if dev == "cuda" else torch.float32,
        )


app = FastAPI(title="Ai-dos — Whisper-kk STT (казахский)")


@app.on_event("startup")
def _warm() -> None:
    # Прогреваем модель при старте, чтобы первый запрос не ждал загрузку весов.
    _load()


@app.get("/health")
def health():
    return {"status": "ok", "model": MODEL, "device": _device(), "loaded": _whisper is not None}


@app.post("/transcribe")
async def transcribe(data: UploadFile = File(...), language: str = Form("kazakh")):
    audio = await data.read()

    def _run() -> str:
        _load()
        speech, sr = sf.read(io.BytesIO(audio), dtype="float32")
        if speech.ndim > 1:          # стерео -> моно
            speech = speech.mean(axis=1)
        if sr != 16000:
            speech = librosa.resample(speech, orig_sr=sr, target_sr=16000)
        out = _whisper(speech, generate_kwargs={"language": "kazakh", "task": "transcribe"})
        return out["text"].strip()

    try:
        # Тяжёлый синтез — в отдельном потоке, чтобы не блокировать event loop.
        text = await asyncio.to_thread(_run)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Whisper-kk error: {e!r}")
    return {"status": "success", "data": text}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)
