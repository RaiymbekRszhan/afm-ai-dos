"""F5-TTS_RUSSIAN — сервер русского TTS (с ударениями через RUAccent).

Запускается ОТДЕЛЬНО (venv .venv-f5: f5-tts + torch + ruaccent), потому что
версии несовместимы с основным API. Основной API зовёт сюда по HTTP — контракт
ТОТ ЖЕ, что у Spark (POST {text, language} -> WAV).

Контракт (его ждёт app/clients/tts.py -> _f5):
    POST /tts   {"text": "...", "language": "russian"}   ->  audio/wav
    GET  /health

Модель: Misha24-10/F5-TTS_RUSSIAN (арх F5TTS_v1_Base). Ударения расставляет
RUAccent (вставляет «+» после ударной гласной) — модель обучена под эту разметку,
это и даёт правильное русское произношение.

Настройки (env):
    F5_MODEL        арх модели для f5-tts. default: "F5TTS_v1_Base"
    F5_CKPT_FILE    путь к чекпойнту (.safetensors). ОБЯЗАТЕЛЕН.
    F5_VOCAB_FILE   путь к vocab.txt (из F5TTS_v1_Base/). ОБЯЗАТЕЛЕН.
    F5_REF_AUDIO    референс-аудио 5-15 c (тембр голоса). ОБЯЗАТЕЛЕН.
    F5_REF_TEXT     транскрипт референса. Пусто -> F5 авто-транскрибирует (Whisper).
    F5_RUACCENT     1|0 — простановка ударений RUAccent. default: 1
    F5_DEVICE       cpu | cuda. default: cpu
    F5_NFE_STEP     шагов диффузии (качество/скорость). default: 32
    F5_SPEED        темп речи. default: 1.0
    F5_PORT         default: 8810
"""
import io
import os

import soundfile as sf
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

MODEL_ARCH = os.environ.get("F5_MODEL", "F5TTS_v1_Base")
CKPT = os.environ.get("F5_CKPT_FILE", "")
VOCAB = os.environ.get("F5_VOCAB_FILE", "")
REF_AUDIO = os.environ.get("F5_REF_AUDIO", "")
REF_TEXT = os.environ.get("F5_REF_TEXT", "")
DEVICE = os.environ.get("F5_DEVICE", "cpu")
NFE = int(os.environ.get("F5_NFE_STEP", "32"))
SPEED = float(os.environ.get("F5_SPEED", "1.0"))
PORT = int(os.environ.get("F5_PORT", "8810"))
USE_ACCENT = os.environ.get("F5_RUACCENT", "1").lower() not in ("0", "false", "no", "")

if not REF_AUDIO:
    raise RuntimeError("F5_REF_AUDIO не задан — F5 требует референс-аудио (5-15 c). См. run.sh.")

from f5_tts.api import F5TTS  # noqa: E402

print(f"[f5] загружаю {MODEL_ARCH} (ckpt={CKPT or 'default'}) на {DEVICE}...")
_model = F5TTS(
    model=MODEL_ARCH,
    ckpt_file=CKPT or "",
    vocab_file=VOCAB or "",
    device=DEVICE,
)
print("[f5] модель готова.")

def _patch_ruaccent_token_type_ids(accentizer) -> list[str]:
    """ruaccent 1.5.8.3: часть ONNX-моделей (accent_model, stress_usage_predictor)
    ТРЕБУЮТ вход token_type_ids, но CharTokenizer его не отдаёт -> onnxruntime падает.
    Добавляем token_type_ids=нули ТОЛЬКО тем сессиям, что его требуют (остальным
    лишний вход тоже ошибка). Изолированный патч, пакет не трогаем."""
    import numpy as np

    patched: list[str] = []
    for name in dir(accentizer):
        sess = getattr(getattr(accentizer, name, None), "session", None)
        if sess is None or not hasattr(sess, "get_inputs"):
            continue
        if "token_type_ids" not in {i.name for i in sess.get_inputs()}:
            continue
        _orig = sess.run

        def run(output_names, input_feed, run_options=None, _orig=_orig):
            if "token_type_ids" not in input_feed and "input_ids" in input_feed:
                input_feed = {**input_feed,
                              "token_type_ids": np.zeros_like(input_feed["input_ids"])}
            return _orig(output_names, input_feed, run_options)

        sess.run = run
        patched.append(name)
    return patched


_accent = None
if USE_ACCENT:
    from ruaccent import RUAccent

    _accent = RUAccent()
    _accent.load(omograph_model_size="turbo3", use_dictionary=True)
    _p = _patch_ruaccent_token_type_ids(_accent)
    print(f"[f5] RUAccent загружен (простановка ударений). token_type_ids-патч: {_p}")

app = FastAPI(title="F5-TTS_RUSSIAN (русский TTS)")


class TTSRequest(BaseModel):
    text: str
    language: str | None = "russian"


@app.get("/health")
def health():
    return {
        "status": "ok",
        "model": MODEL_ARCH,
        "accent": bool(_accent),
        "device": DEVICE,
        "ref_text": bool(REF_TEXT),
    }


@app.post("/tts")
def synthesize(req: TTSRequest):
    if not req.text.strip():
        raise HTTPException(status_code=400, detail="Пустой текст")
    gen_text = req.text
    ref_text = REF_TEXT
    if _accent is not None:
        # Ударения ставим и в озвучиваемый текст, и в транскрипт референса
        # (модель обучена на тексте с «+», так согласованнее).
        gen_text = _accent.process_all(gen_text)
        if ref_text:
            ref_text = _accent.process_all(ref_text)
    wav, sr, _ = _model.infer(
        ref_file=REF_AUDIO,
        ref_text=ref_text,  # "" -> F5 авто-транскрибирует референс через Whisper
        gen_text=gen_text,
        nfe_step=NFE,
        speed=SPEED,
    )
    buf = io.BytesIO()
    sf.write(buf, wav, sr, format="WAV", subtype="PCM_16")
    return Response(content=buf.getvalue(), media_type="audio/wav")


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)
