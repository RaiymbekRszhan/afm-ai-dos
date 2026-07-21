"""STT с роутингом по языку:
  - казахский  -> локальный Whisper-kk (transformers), если включён
  - остальное  -> сервер АФМ (multipart /transcribe)
"""
import asyncio
import io
import threading

import httpx

from app.config import settings

_whisper = None  # ленивый кэш pipeline
_whisper_lock = threading.Lock()  # чтобы не грузить модель дважды (прогрев + запрос)


def _is_kk(language: str | None) -> bool:
    lang = (language or "").lower()
    return lang.startswith(("kaz", "kk", "kz", "қаз", "каз"))


def _device() -> str:
    if settings.whisper_device != "auto":
        return settings.whisper_device
    import torch
    if torch.cuda.is_available():
        return "cuda"
    if torch.backends.mps.is_available():
        return "mps"
    return "cpu"


async def transcribe(audio_bytes: bytes, filename: str, language: str | None = None,
                     content_type: str = "audio/wav") -> str:
    """Аудио -> текст. Маршрутизирует по языку."""
    lang = language or settings.stt_default_language
    if _is_kk(lang):
        # Удалённый Whisper-сервер (на GPU АФМ) приоритетнее локального in-process
        # Whisper — тогда киоск/оркестратор не тянут torch/веса. Контракт совпадает
        # со STT-сервером АФМ, поэтому идём тем же _afm, только на свой адрес.
        if settings.stt_kk_url:
            return await _afm(audio_bytes, filename, "kazakh", content_type,
                              url=settings.stt_kk_url)
        if settings.stt_kk_use_whisper:
            return await _whisper_kk(audio_bytes)
    return await _afm(audio_bytes, filename, lang, content_type)


# ---------- Казахский: локальный Whisper-kk ----------
def load_whisper() -> None:
    """Загружает Whisper-kk в память (один раз). Зовётся при прогреве и в _whisper_kk."""
    global _whisper
    if _whisper is not None:
        return
    # Double-checked locking: прогрев (фоновый поток) и первый kk-запрос (другой
    # поток через to_thread) иначе оба увидят None и загрузят модель дважды
    # (гигабайты -> скачок памяти/OOM).
    with _whisper_lock:
        if _whisper is not None:
            return
        import torch
        from transformers import pipeline
        from transformers.utils import logging as hf_logging
        # Глушим info/warning Whisper (logits processor, clean_up_tokenization,
        # pad_token_id и т.п.) — это шум, на распознавание не влияет.
        hf_logging.set_verbosity_error()
        dev = _device()
        _whisper = pipeline(
            "automatic-speech-recognition",
            model=settings.whisper_kk_model,
            device=dev,
            # длинное аудио: Whisper иначе режет на 30 сек и теряет хвост —
            # chunk_length_s включает скользящее окно с батчингом.
            chunk_length_s=30,
            batch_size=8,
            # на GPU быстрее и экономнее в float16, на CPU/MPS — float32
            dtype=torch.float16 if dev == "cuda" else torch.float32,
        )


async def warmup() -> None:
    """Прогрев в фоне, чтобы первый казахский запрос не тормозил."""
    await asyncio.to_thread(load_whisper)


async def _whisper_kk(audio_bytes: bytes) -> str:
    def _run() -> str:
        load_whisper()
        import librosa
        import soundfile as sf
        speech, sr = sf.read(io.BytesIO(audio_bytes), dtype="float32")
        if speech.ndim > 1:           # стерео -> моно
            speech = speech.mean(axis=1)
        if sr != 16000:
            speech = librosa.resample(speech, orig_sr=sr, target_sr=16000)
        out = _whisper(speech, generate_kwargs={"language": "kazakh", "task": "transcribe"})
        return out["text"].strip()

    return await asyncio.to_thread(_run)


# ---------- Остальные языки: сервер АФМ ----------
async def _afm(audio_bytes: bytes, filename: str, language: str,
               content_type: str, url: str | None = None) -> str:
    # url=None -> STT-сервер АФМ (settings.stt_url, русский/фолбэк). Для казахского
    # с удалённым Whisper передаётся settings.stt_kk_url — контракт тот же.
    files = {"data": (filename, audio_bytes, content_type)}
    data = {"language": language}
    async with httpx.AsyncClient(timeout=300) as client:
        resp = await client.post(url or settings.stt_url, files=files, data=data,
                                 headers={"accept": "application/json"})
        resp.raise_for_status()
        payload = resp.json()

    # Формат сервера АФМ: {"status": "success", "data": "<текст>"}
    if isinstance(payload, dict):
        status = payload.get("status")
        if status and status != "success":
            raise RuntimeError(f"STT вернул статус '{status}': {payload}")
        if "data" in payload:
            data = payload["data"]
            # Защита от {"data": null}: иначе .strip() у вызывающего даст
            # AttributeError -> 500 вместо аккуратного 502.
            if not isinstance(data, str):
                raise RuntimeError(f"STT вернул нестроковый data: {payload!r}")
            return data
        for key in ("text", "transcription", "result", "transcript"):
            if key in payload:
                value = payload[key]
                if isinstance(value, str):
                    return value
    if isinstance(payload, str):
        return payload
    raise RuntimeError(f"Неожиданный формат ответа STT: {payload!r}")
