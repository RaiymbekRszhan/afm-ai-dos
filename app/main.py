import os
from contextlib import asynccontextmanager
from urllib.parse import quote

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from app import service
from app.clients import rag, stt, tts
from app.config import settings
from app.schemas import ChatRequest, ChatResponse, SpeakRequest, TranscribeResponse

_STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")


@asynccontextmanager
async def lifespan(app: FastAPI):
    import asyncio
    # Источник ответа — внешний RAG-сервис (:8077). Локальный индекс больше не грузим.
    # Прогрев Whisper-kk в фоне, чтобы первый казахский запрос не тормозил.
    if settings.stt_kk_use_whisper:
        print("[startup] прогреваю Whisper-kk в фоне...")
        asyncio.create_task(stt.warmup())
    yield


async def _read_upload(data: UploadFile) -> bytes:
    """Читает загруженное аудио с ограничением размера (защита от перегруза памяти).

    Сначала проверяем известный размер (data.size заполнен парсером ДО чтения в
    память) — чтобы не тянуть гигантский файл в RAM. Затем бэкстоп после чтения
    (если size неизвестен).
    """
    limit = settings.max_upload_mb * 1024 * 1024

    def _too_big(n: int) -> HTTPException:
        return HTTPException(
            status_code=413,
            detail=f"Файл больше {settings.max_upload_mb} МБ ({n // (1024 * 1024)} МБ)",
        )

    if data.size is not None and data.size > limit:
        raise _too_big(data.size)
    audio = await data.read()
    if len(audio) > limit:
        raise _too_big(len(audio))
    return audio


# docs_url=None — отключаем встроенный Swagger (он тянет CSS/JS с CDN, а в сети
# АФМ нет интернета). Ниже отдаём Swagger с локальных файлов.
app = FastAPI(title="АФМ — Цифровой офицер Ai-dos", lifespan=lifespan, docs_url=None,
              redoc_url=None)

app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")


@app.get("/", include_in_schema=False)
async def root():
    return FileResponse(os.path.join(_STATIC_DIR, "index.html"))


@app.get("/ui", include_in_schema=False)
async def ui():
    return FileResponse(os.path.join(_STATIC_DIR, "index.html"))


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    """Иконка вкладки — чтобы браузер не сыпал 404 на /favicon.ico."""
    path = os.path.join(_STATIC_DIR, "swagger-ui", "favicon-32x32.png")
    if os.path.exists(path):
        return FileResponse(path, media_type="image/png")
    return Response(status_code=204)


@app.get("/docs", include_in_schema=False)
async def custom_docs():
    """Swagger UI с локальными ассетами (работает без интернета)."""
    return get_swagger_ui_html(
        openapi_url=app.openapi_url,
        title=app.title,
        swagger_js_url="/static/swagger-ui/swagger-ui-bundle.js",
        swagger_css_url="/static/swagger-ui/swagger-ui.css",
        swagger_favicon_url="/static/swagger-ui/favicon-32x32.png",
    )


@app.get("/health")
async def health():
    """Статус оркестратора + доступность RAG-сервиса (источника ответа)."""
    return {
        "status": "ok",
        "rag": await rag.healthy(),
        "tts": {
            "enabled": settings.tts_enabled,
            "ru": settings.tts_provider,
            "kk": settings.tts_kk_provider,
            "servers": await tts.healthy(),
        },
        "stt_kk_whisper": settings.stt_kk_use_whisper,
    }


@app.post("/transcribe", response_model=TranscribeResponse)
async def transcribe_endpoint(
    data: UploadFile = File(...),
    language: str = Form(default=None),
    correct: bool = Form(default=None),
):
    """Шаг STT: аудио → текст (+ опциональная LLM-коррекция)."""
    audio = await _read_upload(data)
    lang = language or settings.stt_default_language
    try:
        raw = await stt.transcribe(
            audio, data.filename or "audio.wav", lang,
            content_type=data.content_type or "audio/wav",
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"STT error: {e}")

    do_correct = service.should_correct(lang) if correct is None else correct
    text = await service.correct_transcript(raw, lang) if do_correct else raw
    return TranscribeResponse(text=text, language=lang, raw_text=raw)


@app.post("/chat", response_model=ChatResponse)
async def chat_endpoint(req: ChatRequest):
    """Шаг RAG: текст вопроса → ответ строго по базе (внешний RAG-сервис :8077)."""
    if not req.question.strip():
        raise HTTPException(status_code=400, detail="Пустой вопрос")
    service.check_injection(req.question, req.language)  # только логируем, не блокируем
    try:
        result = await rag.ask(req.question, req.language, with_sources=True)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"RAG error: {e}")
    return result


@app.post("/speak")
async def speak_endpoint(req: SpeakRequest):
    """Шаг TTS: текст → аудио на нужном языке."""
    try:
        audio = await tts.synthesize(req.text, req.language)
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"TTS error: {e}")
    return Response(content=audio, media_type=f"audio/{settings.tts_format}")


@app.post("/voice")
async def voice_endpoint(
    data: UploadFile = File(...),
    language: str = Form(default=None),
):
    """Полный пайплайн: аудио → STT → RAG+LLM → (TTS).

    Возвращает аудио-ответ, если TTS включён; иначе JSON с текстом.
    """
    audio = await _read_upload(data)
    lang = language or settings.stt_default_language
    try:
        question = await stt.transcribe(
            audio, data.filename or "audio.wav", lang,
            content_type=data.content_type or "audio/wav",
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"STT error: {e}")

    if service.should_correct(lang):
        question = await service.correct_transcript(question, lang)

    if not question.strip():
        raise HTTPException(status_code=400, detail="Не удалось распознать речь. Повторите вопрос.")

    service.check_injection(question, lang)  # только логируем, не блокируем

    try:
        # sources не нужны в озвучке: with_sources=True заставил бы RAG делать
        # второй запрос к графу/LLM на каждый вопрос — лишняя задержка.
        result = await rag.ask(question, lang, with_sources=False)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"RAG error: {e}")

    answer = result["answer"]

    if settings.tts_enabled:
        try:
            out_audio = await tts.synthesize(answer, lang)
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"TTS error: {e}")
        return Response(
            content=out_audio,
            media_type=f"audio/{settings.tts_format}",
            headers={
                # percent-encode: HTTP-заголовки только latin-1, а текст русский/казахский.
                # UI может показать и вопрос, и текст озвученного ответа.
                "X-Question": quote(question),
                "X-Answer": quote(answer),
            },
        )

    return {"question": question, "answer": answer, "sources": result["sources"]}
