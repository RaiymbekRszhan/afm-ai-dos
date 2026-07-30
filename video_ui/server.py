"""Ai-dos — киоск-страница «видео-аватар» (:8100), выбранный фронтенд пилота.

Пока тихо — крутится idle-ролик (аватар стоит); задали вопрос — играет WAV
живого ответа (STT→RAG→TTS с основного бэкенда) и параллельно немой ролик
«аватар говорит», зациклённый на длину звука. Липсинк не нужен (решение
руководителя). Поверх — таблицы и графики из базы, QR-коды порталов,
караоке-подсветка, увеличение по тапу (см. static/).

Почему отдельное приложение на своём порту (:8100):
  * бэкенд (:8000) остаётся чистым пайплайном без киоск-логики;
  * вопросы ПРОКСИРУЕМ на :8000 (см. /voice) — свой STT/RAG/TTS не поднимаем;
  * прокси (а не прямой запрос из браузера на :8000) нужен, чтобы страница и её
    фетчи были same-origin: иначе в бэкенд пришлось бы добавлять CORS.

Запуск: bash run_api.sh (весь стек) или bash video_ui/run.sh (только страница).
"""

import json
import os

import httpx
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

_DIR = os.path.dirname(os.path.abspath(__file__))
_STATIC_DIR = os.path.join(_DIR, "static")
_VIDEO_DIR = os.path.join(_STATIC_DIR, "video")

# Рабочий бэкенд (:8000), куда проксируем вопросы.
BACKEND = os.environ.get("AIDOS_BACKEND", "http://localhost:8000").rstrip("/")
# /voice — самый долгий путь (STT + RAG + TTS). На GPU ~секунды, на CPU-TTS — минуты,
# поэтому таймаут щедрый: лучше подождать, чем оборвать ответ на демонстрации.
# ДОЛЖЕН быть заметно больше app.config.settings.rag_timeout (260 с) — иначе этот
# прокси-таймаут срабатывает раньше, чем бэкенд успевает честно дождаться RAG, и
# гражданин получает обрыв здесь вместо ответа. Запас (300 -> 450) сохраняет тот
# же бюджет на STT+TTS сверх RAG, что был при старом rag_timeout=120.
BACKEND_TIMEOUT = float(os.environ.get("AIDOS_BACKEND_TIMEOUT", "450"))
# video_ui слушает 0.0.0.0 (см. run.sh) — тот же уровень сетевой доступности, что
# и бэкенд на :8000, у которого уже есть свой лимит (app/config.py max_upload_mb).
# Без своего лимита здесь любой клиент в сети киоска мог целиком забуферить
# многогигабайтное тело в память ДО того, как бэкенд успеет его отклонить.
MAX_UPLOAD_MB = float(os.environ.get("AIDOS_MAX_UPLOAD_MB", "25"))

app = FastAPI(title="Ai-dos — киоск «видео-аватар»", docs_url=None, redoc_url=None)


@app.middleware("http")
async def _no_stale_assets(request, call_next):
    """Страница и её код (html/js/css) — всегда свежие, без ручной очистки кэша.

    StaticFiles не шлёт Cache-Control, поэтому браузер эвристически кэшировал
    старый answer_render.js и по F5 его НЕ перечитывал — правки (QR над аватаром)
    на киоске «не применялись». `no-cache` заставляет браузер каждый раз
    сверяться с сервером (ETag → 304, если не менялся: дёшево, файлы локальные) и
    подхватывать новую версию сразу. Ролики (.mp4) НЕ трогаем — они тяжёлые,
    пусть кэшируются, иначе перекачивались бы на каждой загрузке страницы.

    ⚠️ Живые данные (`/health`, `/admin/*`) — `no-store`, и это НЕ перестраховка.
    Регионы ходят на сервер через КЭШИРУЮЩИЙ прокси АФМ (Squid
    `ntp2.afm.gov.kz:3128`, подтверждено 30.07 заголовками `X-Cache` /
    `X-Cache-Lookup` в ответе). У этих ответов нет ни ETag, ни Cache-Control,
    зато есть 200 и Content-Length — то есть ровно то, что Squid по эвристике
    вправе закэшировать. Последствия: киоск получил бы бодрый 200 на /health,
    когда сервер уже лежит, и открыл мёртвую страницу; админка показала бы
    «на связи» у молчащей точки. POST (`/voice`, `/kiosk/ping`) прокси не
    кэширует, поэтому под удар попадают именно проверки и админка.
    Страница `/admin` тоже сюда попадает: путь без `.html`, и раньше она не
    получала вообще никакого заголовка."""
    response = await call_next(request)
    path = request.url.path
    if path == "/" or path.endswith((".html", ".js", ".css")):
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
    elif path == "/health" or path.startswith("/admin"):
        response.headers["Cache-Control"] = "no-store"
    return response


app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")


def _videos_present() -> dict:
    """Лежат ли ролики на месте — частая причина «чёрного экрана» на демо.

    Сверяем имена ТОЧНО (listdir), а не через os.path.exists: на macOS файловая
    система регистро-независимая, и exists("idle.mp4") вернёт True для лежащего
    рядом "Idle.mp4". На Linux регистр важен — браузер получит 404, и проверка,
    прошедшая на маке, промолчала бы ровно там, где ломается (сервер АФМ)."""
    try:
        real = set(os.listdir(_VIDEO_DIR))
    except OSError:
        real = set()
    return {name: name in real for name in ("idle.mp4", "talk.mp4")}


@app.get("/", include_in_schema=False)
async def root():
    return FileResponse(os.path.join(_STATIC_DIR, "index.html"))


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    """Иконка вкладки — чтобы браузер не сыпал 404 в лог. Берём ту же, что и
    основной вариант (:8000), иначе 204 «пусто, и не спрашивай больше»."""
    path = os.path.join(os.path.dirname(_DIR), "app", "static", "swagger-ui",
                        "favicon-32x32.png")
    if os.path.exists(path):
        return FileResponse(path, media_type="image/png")
    return Response(status_code=204)


@app.get("/admin", include_in_schema=False)
async def admin_page():
    """Админка «статус + рубильник».

    Живёт здесь, а не на :8000, по вынужденной причине: оркестратор на сервере
    закрыт на 127.0.0.1, и снаружи его никто не откроет. Сама проверка токена —
    на бэкенде: страница только рисует, решает доступ он.
    """
    return FileResponse(os.path.join(_STATIC_DIR, "admin.html"))


@app.post("/kiosk/ping")
async def kiosk_ping(kiosk: str = Form(default=None), key: str = Form(default=None)):
    """Heartbeat киоска — насквозь на бэкенд. Сбой сети не должен ломать
    страницу: молчим 503, страница просто попробует в следующий раз."""
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(BACKEND + "/kiosk/ping", data={"kiosk": kiosk or "", "key": key or ""})
    except Exception:
        raise HTTPException(status_code=503, detail="Бэкенд недоступен.")
    return Response(content=r.content, status_code=r.status_code,
                    media_type=r.headers.get("content-type", "application/json"))


@app.api_route("/admin/{path:path}", methods=["GET", "POST"], include_in_schema=False)
async def admin_api(path: str, request: Request):
    """API админки — насквозь на бэкенд вместе с токеном.

    Один маршрут на всё поддерево, а не по одному на эндпоинт: иначе каждый
    новый отчёт требовал бы правки ещё и здесь, и рассинхрон нашёлся бы уже
    на сервере. Токен НЕ проверяем здесь — единственный источник правды
    оркестратор: два места принятия решения о доступе неизбежно разъезжаются.
    """
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            if request.method == "GET":
                r = await c.get(f"{BACKEND}/admin/{path}",
                                params=dict(request.query_params))
            else:
                r = await c.post(f"{BACKEND}/admin/{path}",
                                 data=dict(await request.form()))
    except Exception as e:
        print(f"[video_ui] backend {BACKEND} недоступен: {e!r}")
        raise HTTPException(status_code=502, detail="Рабочий бэкенд недоступен.")
    # Content-Disposition нужен выгрузу CSV: без него браузер покажет файл
    # текстом вместо «Сохранить как».
    headers = {}
    disp = r.headers.get("content-disposition")
    if disp:
        headers["Content-Disposition"] = disp
    return Response(content=r.content, status_code=r.status_code, headers=headers,
                    media_type=r.headers.get("content-type", "application/json"))


@app.get("/health")
async def health():
    """Жив ли демо-сервер, виден ли ему рабочий бэкенд и на месте ли ролики."""
    backend_reachable = False
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            backend_reachable = (await c.get(BACKEND + "/health")).status_code == 200
    except Exception:
        pass
    return {"status": "ok", "backend": BACKEND,
            "backend_reachable": backend_reachable, "videos": _videos_present()}


@app.post("/voice")
async def voice(data: UploadFile = File(...), language: str = Form("russian"),
                suggest: str = Form(default=None), kiosk: str = Form(default=None),
                key: str = Form(default=None)):
    """Вопрос → рабочий бэкенд (:8000) → JSON-ответ (текст + аудио в base64).

    Тело прокидываем как есть: контракт /voice бэкенда — JSON
    {question, answer, suggest, print, provider, format, audio_b64} (N5).
    `suggest` — подсказка «возможно, вы хотели спросить…», страница вернёт её
    полем suggest со следующим вопросом."""
    limit = int(MAX_UPLOAD_MB * 1024 * 1024)

    def _too_big(n: int) -> HTTPException:
        return HTTPException(
            status_code=413,
            detail=f"Файл больше {MAX_UPLOAD_MB:g} МБ ({n // (1024 * 1024)} МБ)",
        )

    if data.size is not None and data.size > limit:
        raise _too_big(data.size)
    audio = await data.read()
    if len(audio) > limit:
        raise _too_big(len(audio))
    files = {"data": (data.filename or "q.wav", audio, data.content_type or "audio/wav")}
    form = {"language": language}
    if suggest:
        form["suggest"] = suggest
    # Номер точки (?id= на странице) — только в аналитику бэкенда; на ответ не влияет.
    if kiosk:
        form["kiosk"] = kiosk
    # Пропуск точки (?key= на странице) — его сверяет бэкенд, здесь только несём.
    if key:
        form["key"] = key
    try:
        async with httpx.AsyncClient(timeout=BACKEND_TIMEOUT) as c:
            r = await c.post(BACKEND + "/voice", files=files, data=form)
    except Exception as e:
        # Клиенту — обобщённо: {e!r} тянет внутренние адреса/детали httpx
        # (раскрытие топологии сети), полная диагностика — только в лог.
        print(f"[video_ui] backend {BACKEND} недоступен: {e!r}")
        raise HTTPException(status_code=502, detail="Рабочий бэкенд недоступен.")
    if r.status_code != 200:
        try:
            detail = r.json().get("detail", r.text[:200])
        except Exception:
            detail = r.text[:200]
        raise HTTPException(status_code=r.status_code, detail=detail)
    # Контракт /voice — JSON-тело {question, answer, suggest, print, provider,
    # format, audio_b64} (N5): текст и аудио в теле, не в заголовках. Пробрасываем
    # как есть.
    return Response(content=r.content,
                    media_type=r.headers.get("content-type", "application/json"))


@app.post("/voice/stream")
async def voice_stream(data: UploadFile = File(...), language: str = Form("russian"),
                       suggest: str = Form(default=None), kiosk: str = Form(default=None),
                       key: str = Form(default=None)):
    """То же, что /voice, но ответ бэкенда льётся ПОТОКОМ (NDJSON) — насквозь.

    Смысл потока в том, что первая строка (текст) и первый кусок озвучки
    доходят до киоска, пока досинтезируется остальное; поэтому здесь нельзя
    буферизовать — читаем и отдаём по мере поступления.
    """
    limit = int(MAX_UPLOAD_MB * 1024 * 1024)
    if data.size is not None and data.size > limit:
        raise HTTPException(status_code=413,
                            detail=f"Файл больше {MAX_UPLOAD_MB:g} МБ")
    audio = await data.read()
    if len(audio) > limit:
        raise HTTPException(status_code=413,
                            detail=f"Файл больше {MAX_UPLOAD_MB:g} МБ")
    files = {"data": (data.filename or "q.wav", audio, data.content_type or "audio/wav")}
    form = {"language": language}
    if suggest:
        form["suggest"] = suggest
    if kiosk:
        form["kiosk"] = kiosk
    if key:
        form["key"] = key

    async def relay():
        # Клиент httpx живёт ВНУТРИ генератора: закроется вместе с потоком (в
        # т.ч. если гражданин ушёл от киоска и браузер оборвал соединение).
        try:
            async with httpx.AsyncClient(timeout=BACKEND_TIMEOUT) as c:
                async with c.stream("POST", BACKEND + "/voice/stream",
                                    files=files, data=form) as r:
                    if r.status_code != 200:
                        body = await r.aread()
                        detail = _error_detail(body, r)
                        yield json.dumps({"type": "error", "detail": detail},
                                         ensure_ascii=False) + "\n"
                        return
                    async for line in r.aiter_lines():
                        if line:
                            yield line + "\n"
        except Exception as e:
            # Наружу — обобщённо (детали httpx раскрывают топологию сети).
            print(f"[video_ui] поток от {BACKEND} оборвался: {e!r}")
            yield json.dumps({"type": "error", "detail": "Рабочий бэкенд недоступен."},
                             ensure_ascii=False) + "\n"

    return StreamingResponse(relay(), media_type="application/x-ndjson",
                             headers={"Cache-Control": "no-store",
                                      "X-Accel-Buffering": "no"})


def _error_detail(body: bytes, resp) -> str:
    try:
        return json.loads(body).get("detail", "") or f"HTTP {resp.status_code}"
    except Exception:
        return f"HTTP {resp.status_code}"
