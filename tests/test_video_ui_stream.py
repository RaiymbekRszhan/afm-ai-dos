"""Прокси киоска (video_ui) для потокового ответа: пробрасывает NDJSON насквозь.

Смысл потока в том, что первый кусок озвучки доходит до киоска, пока
досинтезируется остальное, — значит прокси НЕ должен копить тело: буферизация
здесь молча убила бы весь выигрыш. Плюс ошибки бэкенда должны приходить строкой
`error`, а не обрывом (статус 200 уже отдан гражданину).

Сети нет: httpx.AsyncClient подменяется заглушкой.
"""
import importlib.util
import json
import os

import pytest
from fastapi.testclient import TestClient

_PATH = os.path.join(os.path.dirname(__file__), "..", "video_ui", "server.py")
_spec = importlib.util.spec_from_file_location("video_ui_server", _PATH)
video_ui = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(video_ui)


class _FakeStream:
    """Ответ httpx.stream: статус + строки NDJSON."""

    def __init__(self, lines, status_code=200):
        self._lines = lines
        self.status_code = status_code

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def aiter_lines(self):
        for line in self._lines:
            yield line

    async def aread(self):
        return json.dumps({"detail": "бэкенд отказал"}).encode()


class _FakeClient:
    def __init__(self, lines=None, status_code=200, boom=None):
        self._lines, self._status, self._boom = lines or [], status_code, boom

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def stream(self, method, url, **kw):
        if self._boom:
            raise self._boom
        return _FakeStream(self._lines, self._status)


def _client(monkeypatch, **kw):
    monkeypatch.setattr(video_ui.httpx, "AsyncClient",
                        lambda *a, **k: _FakeClient(**kw))
    return TestClient(video_ui.app)


def _post(c):
    return c.post("/voice/stream", files={"data": ("q.wav", b"RIFFfake", "audio/wav")},
                  data={"language": "russian"})


def test_proxy_relays_events_in_order(monkeypatch):
    lines = [json.dumps({"type": "meta", "answer": "Ответ"}, ensure_ascii=False),
             json.dumps({"type": "audio", "seq": 0}),
             json.dumps({"type": "end"})]
    r = _post(_client(monkeypatch, lines=lines))

    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/x-ndjson")
    # nginx перед киоском иначе копит ответ целиком и съедает смысл потока
    assert r.headers.get("x-accel-buffering") == "no"
    events = [json.loads(x) for x in r.text.splitlines() if x.strip()]
    assert [e["type"] for e in events] == ["meta", "audio", "end"]
    assert events[0]["answer"] == "Ответ"


def test_proxy_turns_backend_error_into_event(monkeypatch):
    r = _post(_client(monkeypatch, lines=[], status_code=502))
    events = [json.loads(x) for x in r.text.splitlines() if x.strip()]
    assert events == [{"type": "error", "detail": "бэкенд отказал"}]


def test_proxy_hides_transport_details(monkeypatch):
    """Наружу — обобщённо: repr исключения httpx тянет внутренние адреса."""
    r = _post(_client(monkeypatch, boom=ConnectionError("connect to 192.168.165.2:8000")))
    events = [json.loads(x) for x in r.text.splitlines() if x.strip()]
    assert events[0]["type"] == "error"
    assert "192.168" not in events[0]["detail"]


def test_proxy_rejects_oversized_upload(monkeypatch):
    c = _client(monkeypatch, lines=[])
    big = b"x" * (int(video_ui.MAX_UPLOAD_MB * 1024 * 1024) + 1)
    r = c.post("/voice/stream", files={"data": ("q.wav", big, "audio/wav")},
               data={"language": "russian"})
    assert r.status_code == 413


# ---------------------------------------------------------------------------
# Номер киоска: страница шлёт его полем `kiosk`, прокси обязан донести до
# бэкенда — иначе в логах 20 точек снова сольются в одну кучу.
# ---------------------------------------------------------------------------

def test_proxy_forwards_kiosk_id(monkeypatch):
    seen = {}

    class _Capturing(_FakeClient):
        def stream(self, method, url, **kw):
            seen.update(kw.get("data") or {})
            return _FakeStream([json.dumps({"type": "end"})])

    monkeypatch.setattr(video_ui.httpx, "AsyncClient", lambda *a, **k: _Capturing())
    c = TestClient(video_ui.app)
    r = c.post("/voice/stream", files={"data": ("q.wav", b"RIFFfake", "audio/wav")},
               data={"language": "russian", "kiosk": "astana-01"})
    assert r.status_code == 200
    assert seen.get("kiosk") == "astana-01"


def test_proxy_omits_kiosk_when_not_sent(monkeypatch):
    """Без номера поле не появляется — бэкенд отличает «не прислали» от пустого."""
    seen = {}

    class _Capturing(_FakeClient):
        def stream(self, method, url, **kw):
            seen.update({"data": kw.get("data") or {}})
            return _FakeStream([json.dumps({"type": "end"})])

    monkeypatch.setattr(video_ui.httpx, "AsyncClient", lambda *a, **k: _Capturing())
    c = TestClient(video_ui.app)
    r = c.post("/voice/stream", files={"data": ("q.wav", b"RIFFfake", "audio/wav")},
               data={"language": "russian"})
    assert r.status_code == 200
    assert "kiosk" not in seen["data"]


# ---------- заголовки кэширования (регионы ходят через кэширующий прокси) ----------
def test_live_data_is_not_cacheable(monkeypatch):
    """Регионы ходят через Squid АФМ (подтверждено 30.07 заголовками X-Cache).

    У /health и /admin/* нет ни ETag, ни Cache-Control, зато есть 200 и
    Content-Length — Squid вправе их закэшировать. Тогда киоск получит бодрый
    200 на /health при лежащем сервере, а админка покажет «на связи» у молчащей
    точки. Поэтому им нужен no-store.
    """
    from fastapi.testclient import TestClient

    import video_ui.server as vs

    with TestClient(vs.app) as c:
        health = c.get("/health")
        assert health.headers.get("cache-control") == "no-store"

        # Страница админки: путь без .html, раньше не получала заголовка вовсе.
        admin = c.get("/admin")
        assert admin.headers.get("cache-control") == "no-store"

        # Код страницы по-прежнему просто перепроверяется (ETag → 304, дёшево).
        page = c.get("/")
        assert page.headers.get("cache-control") == "no-cache, must-revalidate"
        js = c.get("/static/admin_util.js")
        assert js.headers.get("cache-control") == "no-cache, must-revalidate"


def test_diag_stream_lines_are_spread_and_padded():
    """Зонд /diag/stream: строки идут ЛЕСЕНКОЙ и достаточно объёмные.

    Он существует, чтобы померить буферизацию прокси НА КИОСКЕ (в админке это
    видно не будет — она считает время на сервере, до прокси). Значит от него
    требуется ровно две вещи: паузы между строками (иначе лесенки нет и мерить
    нечего) и объём строки, сравнимый с куском озвучки — буферизация обычно
    пороговая по объёму (Squid `read_ahead_gap`, 16 КБ по умолчанию), и на
    коротких строках прокси прошёл бы тест, а на реальном звуке застрял.
    """
    import video_ui.server as vs

    with TestClient(vs.app) as c:
        # POST — как боевой /voice/stream: прокси обращается с методами по-разному.
        r = c.post("/diag/stream?n=3&gap_ms=50&size=4096")
        assert r.status_code == 200
        assert r.headers.get("cache-control") == "no-store"
        assert r.headers.get("x-accel-buffering") == "no"

        rows = [json.loads(x) for x in r.text.splitlines() if x.strip()]
        assert [x["i"] for x in rows] == [0, 1, 2]
        # Часы сервера показывают именно паузы, а не мгновенную выдачу.
        assert rows[-1]["server_ms"] >= 80
        # Добивка доводит строку до запрошенного объёма (±перевод строки).
        assert all(len(json.dumps(x, ensure_ascii=False)) == 4096 for x in rows)


def test_diag_stream_clamps_absurd_params():
    """Параметры зажаты: страница служебная, но открыта всем в сети киоска.

    Без потолка `?n=100000&size=1000000` превратил бы диагностику в способ
    занять сервер на час и выесть канал региона.
    """
    import video_ui.server as vs

    with TestClient(vs.app) as c:
        r = c.post("/diag/stream?n=99999&gap_ms=0&size=99999999")
        rows = [json.loads(x) for x in r.text.splitlines() if x.strip()]
        assert len(rows) == 40
        assert len(json.dumps(rows[0], ensure_ascii=False)) == 262144
