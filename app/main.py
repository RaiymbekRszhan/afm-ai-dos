import asyncio
import logging
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

log = logging.getLogger("ai_dos.api")

_STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")

# Ограничитель одновременных /voice (дорогой путь): защита TTS/GPU от перегруза.
_voice_sem = asyncio.Semaphore(max(1, settings.max_concurrent_voice))
# Ссылка на фоновый прогрев Whisper — иначе GC может убить task до завершения.
_warmup_task: asyncio.Task | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _warmup_task
    # Источник ответа — внешний RAG-сервис (:8077). Локальный индекс больше не грузим.
    # Прогрев Whisper-kk в фоне, чтобы первый казахский запрос не тормозил.
    if settings.stt_kk_use_whisper:
        print("[startup] прогреваю Whisper-kk в фоне...")
        _warmup_task = asyncio.create_task(stt.warmup())
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
        # Полную ошибку — в лог; клиенту — обобщённо (в тексте httpx-ошибок ходят
        # внутренние адреса сервисов АФМ, это раскрытие топологии сети).
        log.warning("STT error [%s]: %r", lang, e)
        raise HTTPException(status_code=502, detail="Ошибка распознавания речи (STT).")

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
        log.warning("RAG error [%s]: %r", req.language, e)
        raise HTTPException(status_code=502, detail="Ошибка поиска по базе (RAG).")
    return result


@app.post("/speak")
async def speak_endpoint(req: SpeakRequest):
    """Шаг TTS: текст → аудио на нужном языке."""
    if not req.text.strip():
        raise HTTPException(status_code=400, detail="Пустой текст")
    try:
        audio = await tts.synthesize(req.text, req.language)
    except RuntimeError as e:
        # RuntimeError здесь — наши собственные сообщения (провайдер не настроен и
        # т.п.), их можно показать; сетевые ошибки идут ниже и обезличиваются.
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        log.warning("TTS error [%s]: %r", req.language, e)
        raise HTTPException(status_code=502, detail="Ошибка синтеза речи (TTS).")
    return Response(content=audio, media_type=f"audio/{settings.tts_format}")


@app.post("/voice")
async def voice_endpoint(
    data: UploadFile = File(...),
    language: str = Form(default=None),
    suggest: str = Form(default=None),
):
    """Полный пайплайн: аудио → STT → RAG+LLM → (TTS).

    Возвращает WAV ответа + текст в заголовках (X-Question/X-Answer) — их
    показывает страница видео-аватара (video_ui). Без TTS — JSON с текстом.

    `suggest` — подсказка с ПРОШЛОГО ответа (заголовок X-Suggest): если STT
    исказил вопрос и RAG не нашёл ответа, мы предлагаем исправленную
    формулировку; страница шлёт её со следующим вопросом, и реплика-согласие
    («да», «иә») означает «отвечай на исправленный вопрос».
    """
    audio = await _read_upload(data)
    lang = language or settings.stt_default_language
    # /voice — самый дорогой путь; ограничиваем число одновременных, чтобы пачка
    # запросов не положила TTS/GPU-ноду. Лишние ждут очереди (не отвергаются).
    async with _voice_sem:
        try:
            question = await stt.transcribe(
                audio, data.filename or "audio.wav", lang,
                content_type=data.content_type or "audio/wav",
            )
        except Exception as e:
            log.warning("STT error [%s]: %r", lang, e)
            raise HTTPException(status_code=502, detail="Ошибка распознавания речи (STT).")

        if service.should_correct(lang):
            question = await service.correct_transcript(question, lang)

        if not question.strip():
            raise HTTPException(status_code=400, detail="Не удалось распознать речь. Повторите вопрос.")

        # «Да» в ответ на наше «возможно, вы хотели спросить …?» — отвечаем на
        # исправленный вопрос. Любая другая реплика — обычный новый вопрос.
        suggest_used = False
        if suggest and service.is_affirmative(question):
            question = suggest
            suggest_used = True

        service.check_injection(question, lang)  # только логируем, не блокируем

        try:
            # sources не нужны в озвучке: with_sources=True заставил бы RAG делать
            # второй запрос к графу/LLM на каждый вопрос — лишняя задержка.
            result = await rag.ask(question, lang, with_sources=False)
        except Exception as e:
            log.warning("RAG error [%s]: %r", lang, e)
            raise HTTPException(status_code=502, detail="Ошибка поиска по базе (RAG).")

        answer = result["answer"]

        # RAG не нашёл ответа — вероятно, STT исказил вопрос. Предлагаем гражданину
        # исправленную формулировку (доп. LLM-вызов ТОЛЬКО на пути отказа). После
        # подтверждённой подсказки повторно не уточняем — иначе цикл уточнений.
        suggestion = None
        if not suggest_used and service.looks_not_found(answer):
            suggestion = await service.suggest_question(question, lang)
            if suggestion:
                answer = service.clarify_phrase(suggestion, lang)

        # Печать образцов: ответ про подачу заявления/жалобы/приём → предлагаем
        # распечатать бланк. Приглашение ДОПИСЫВАЕМ в ответ (аватар проговорит +
        # уйдёт в X-Answer на экран), id образцов — в заголовок X-Print. На пути
        # уточнения (suggestion) не предлагаем — там ещё не ответ по существу.
        print_ids: list[str] = []
        if not suggestion:
            print_ids = service.detect_print_templates(answer)
            if print_ids:
                answer = service.with_print_offer(answer, lang)

        if settings.tts_enabled:
            try:
                out_audio = await tts.synthesize(answer, lang)
            except Exception as e:
                log.warning("TTS error [%s]: %r", lang, e)
                raise HTTPException(status_code=502, detail="Ошибка синтеза речи (TTS).")
            headers = {
                # percent-encode: HTTP-заголовки только latin-1, а текст русский/казахский.
                # UI может показать и вопрос, и текст озвученного ответа.
                "X-Question": quote(question),
                "X-Answer": quote(answer),
            }
            if suggestion:
                # страница вернёт это в поле `suggest` следующего запроса
                headers["X-Suggest"] = quote(suggestion)
            if print_ids:
                # страница покажет кнопку печати и построит меню образцов
                headers["X-Print"] = ",".join(print_ids)
            return Response(
                content=out_audio,
                media_type=f"audio/{settings.tts_format}",
                headers=headers,
            )

        out = {"question": question, "answer": answer, "sources": result["sources"]}
        if suggestion:
            out["suggest"] = suggestion
        if print_ids:
            out["print"] = print_ids
        return out
