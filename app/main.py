import asyncio
import html
import logging
import os
import time
from contextlib import asynccontextmanager
from urllib.parse import quote

from fastapi import FastAPI, File, Form, Header, HTTPException, Query, UploadFile
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles

from app import service
from app.clients import rag, stt, tts
from app.config import settings
from app.schemas import ChatRequest, ChatResponse, SpeakRequest, TranscribeResponse

log = logging.getLogger("ai_dos.api")

_STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")

# Отладочный «блокнот»: страница /debug/log, куда можно вставить текст из Output
# Log Unreal и сохранить — ложится в этот файл (его читает разработчик/Claude).
_DEBUG_LOG = os.path.join(os.path.dirname(os.path.dirname(__file__)), "debug", "ue_log.txt")

# Кэш последнего озвученного ответа: рендер-нода (Unreal) забирает его через
# GET /last_answer после того, как вопрос задали голосом в веб-UI (/voice).
_last_answer: dict = {}
# long-poll: /last_answer/wait висит на этом событии до появления нового ответа
_answer_event = asyncio.Event()
# Метка последнего опроса рендер-нодой (/last_answer*). Нода в авторежиме watch()
# опрашивает /wait каждые ≤30 с, поэтому долгая тишина = нода упала/зависла.
# По этой метке /health показывает состояние аватара, а watchdog на Windows-ноде
# решает, пора ли перезапускать редактор (см. unreal/watchdog.ps1).
_node_seen: dict = {"ts": None}
# Порог «нода жива»: 3 цикла long-poll с запасом на ретраи после ошибок сети.
_NODE_ALIVE_SEC = 90.0
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

_UNREAL_SCRIPT = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                              "unreal", "aidos_editor.py")


@app.get("/static/aidos_editor.py", include_in_schema=False)
async def unreal_script():
    """Скрипт для рендер-ноды раздаётся из unreal/ напрямую — единственный
    источник правды, копий в static/ держать не нужно. Маршрут объявлен ДО
    mount("/static"), поэтому перекрывает статику."""
    return FileResponse(_UNREAL_SCRIPT, media_type="text/x-python")


@app.get("/static/init_unreal.py", include_in_schema=False)
async def unreal_init_script():
    """Автозапуск авторежима на ноде (кладётся рядом с aidos_editor.py)."""
    path = os.path.join(os.path.dirname(_UNREAL_SCRIPT), "init_unreal.py")
    return FileResponse(path, media_type="text/x-python")


app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")


@app.get("/", include_in_schema=False)
async def root():
    return FileResponse(os.path.join(_STATIC_DIR, "index.html"))


@app.get("/ui", include_in_schema=False)
async def ui():
    return FileResponse(os.path.join(_STATIC_DIR, "index.html"))


@app.get("/avatar", include_in_schema=False)
async def avatar_ui():
    """Гражданский фронтенд: живой аватар (Pixel Streaming с рендер-ноды) + микрофон."""
    return FileResponse(os.path.join(_STATIC_DIR, "avatar.html"))


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


def _node_status() -> dict:
    """Жива ли рендер-нода: давность последнего опроса /last_answer* и вывод."""
    ts = _node_seen["ts"]
    ago = None if ts is None else round(time.time() - ts, 1)
    return {
        "watching": ago is not None and ago < _NODE_ALIVE_SEC,
        "last_poll_ago_sec": ago,
    }


@app.get("/health")
async def health():
    """Статус оркестратора + доступность RAG-сервиса (источника ответа)."""
    return {
        "status": "ok",
        "rag": await rag.healthy(),
        "node": _node_status(),
        "tts": {
            "enabled": settings.tts_enabled,
            "ru": settings.tts_provider,
            "kk": settings.tts_kk_provider,
            "servers": await tts.healthy(),
        },
        "stt_kk_whisper": settings.stt_kk_use_whisper,
    }


# --- Отладочный «блокнот» для логов ноды (временный, можно убрать после отладки) ---
_DEBUG_PAGE = """<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Ai-dos — журнал для отладки</title>
<style>
 body{font-family:-apple-system,Segoe UI,Roboto,sans-serif;background:#0f172a;color:#e2e8f0;margin:0;padding:20px}
 h1{font-size:18px;margin:0 0 6px} .muted{color:#94a3b8;font-size:13px;margin:0 0 12px}
 textarea{width:100%;height:55vh;background:#1e293b;color:#e2e8f0;border:1px solid #334155;
   border-radius:8px;padding:10px;font-family:ui-monospace,Menlo,Consolas,monospace;font-size:13px;box-sizing:border-box}
 button{background:#3b62f6;color:#fff;border:0;border-radius:8px;padding:11px 22px;font-size:15px;cursor:pointer;margin-top:10px}
 .ok{color:#22c55e;font-size:14px;margin-left:12px}
</style></head><body>
 <h1>Журнал Unreal для отладки</h1>
 <p class="muted">Вставь текст из Output Log (UE) и нажми «Сохранить». Потом скажи в чате — я его прочитаю.__STATUS__</p>
 <form method="post" action="/debug/log">
   <textarea name="text" placeholder="Вставь сюда лог из Output Log Unreal...">__CONTENT__</textarea><br>
   <button type="submit">Сохранить</button>
 </form>
</body></html>"""


def _debug_render(status: str = "") -> str:
    saved = ""
    if os.path.exists(_DEBUG_LOG):
        with open(_DEBUG_LOG, encoding="utf-8") as f:
            saved = f.read()
    return (_DEBUG_PAGE
            .replace("__STATUS__", status)
            .replace("__CONTENT__", html.escape(saved)))


@app.get("/debug/log", include_in_schema=False)
async def debug_log_page():
    """Страница-блокнот: вставить сюда лог ноды и сохранить в файл на Маке."""
    return HTMLResponse(_debug_render())


@app.post("/debug/log", include_in_schema=False)
async def debug_log_save(text: str = Form(default="")):
    """Сохраняет вставленный лог в файл (перезаписывает предыдущий)."""
    os.makedirs(os.path.dirname(_DEBUG_LOG), exist_ok=True)
    with open(_DEBUG_LOG, "w", encoding="utf-8") as f:
        f.write(text)
    return HTMLResponse(_debug_render(
        status=f' <span class="ok">✓ сохранено ({len(text)} симв.)</span>'))


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
):
    """Полный пайплайн: аудио → STT → RAG+LLM → (TTS).

    Возвращает аудио-ответ, если TTS включён; иначе JSON с текстом.
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

        service.check_injection(question, lang)  # только логируем, не блокируем

        try:
            # sources не нужны в озвучке: with_sources=True заставил бы RAG делать
            # второй запрос к графу/LLM на каждый вопрос — лишняя задержка.
            result = await rag.ask(question, lang, with_sources=False)
        except Exception as e:
            log.warning("RAG error [%s]: %r", lang, e)
            raise HTTPException(status_code=502, detail="Ошибка поиска по базе (RAG).")

        answer = result["answer"]

        if settings.tts_enabled:
            try:
                out_audio = await tts.synthesize(answer, lang)
            except Exception as e:
                log.warning("TTS error [%s]: %r", lang, e)
                raise HTTPException(status_code=502, detail="Ошибка синтеза речи (TTS).")
            _last_answer.update(audio=out_audio, question=question, answer=answer,
                                id=str(time.time()))
            _answer_event.set()  # разбудить висящие long-poll'ы ноды
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


def _check_node_token(token: str | None) -> None:
    """Опциональная защита эндпоинтов ноды. Если LAST_ANSWER_TOKEN задан —
    требуем его в заголовке X-Aidos-Token (иначе 401). Пусто = проверка выключена
    (обратная совместимость: старая нода без токена продолжает работать).

    Успешный вызов заодно обновляет heartbeat ноды (_node_seen): через него
    проходят все /last_answer*, других клиентов у этих эндпоинтов нет."""
    expected = settings.last_answer_token
    if expected and token != expected:
        raise HTTPException(status_code=401, detail="Требуется токен доступа ноды.")
    _node_seen["ts"] = time.time()


@app.get("/last_answer/id")
async def last_answer_id(x_aidos_token: str | None = Header(default=None)):
    """Лёгкая проверка «есть ли новый ответ» — рендер-нода опрашивает её в цикле,
    не выкачивая WAV целиком."""
    _check_node_token(x_aidos_token)
    return {"id": _last_answer.get("id")}


@app.get("/last_answer/wait")
async def last_answer_wait(
    since: str = "",
    # ge/le отсекают и out-of-range, и NaN (nan >= 1 -> False -> 422): иначе
    # asyncio.wait_for(timeout=nan) кладёт NaN в heap таймеров event loop.
    timeout: float = Query(default=25, ge=1, le=55),
    x_aidos_token: str | None = Header(default=None),
):
    """Long-poll: держим запрос, пока не появится ответ новее `since` (или до
    `timeout` секунд). Нода узнаёт о готовом ответе мгновенно, без 5-сек опроса."""
    _check_node_token(x_aidos_token)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while _last_answer.get("id", "") in ("", since):
        remaining = deadline - loop.time()
        if remaining <= 0:
            break
        _answer_event.clear()
        try:
            await asyncio.wait_for(_answer_event.wait(), timeout=remaining)
        except asyncio.TimeoutError:
            break
    return {"id": _last_answer.get("id")}


@app.get("/last_answer")
async def last_answer(x_aidos_token: str | None = Header(default=None)):
    """Последний ответ /voice (WAV + текст в заголовках) — для рендер-ноды.

    X-Id меняется с каждым новым ответом: клиент может отличить свежий ответ
    от уже проигранного.
    """
    _check_node_token(x_aidos_token)
    if not _last_answer:
        raise HTTPException(status_code=404, detail="Ответов ещё не было")
    return Response(
        content=_last_answer["audio"],
        media_type=f"audio/{settings.tts_format}",
        headers={
            "X-Question": quote(_last_answer["question"]),
            "X-Answer": quote(_last_answer["answer"]),
            "X-Id": _last_answer["id"],
        },
    )
