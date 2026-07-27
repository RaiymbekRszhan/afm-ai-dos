"""Health-проба TTS (N1 + N4): сервер без /health считается живым по факту
HTTP-ответа (multipart-F5 на GPU отвечает 404, но ЖИВ), ElevenLabs — по 2xx с кэшем.
"""
import asyncio

from app.clients import tts


class _Resp:
    def __init__(self, status_code):
        self.status_code = status_code


class _FakeClient:
    """Минимальный async-клиент: .get вызывает переданный обработчик."""

    def __init__(self, handler):
        self._handler = handler

    async def get(self, url, headers=None):
        return self._handler(url, headers)


# --- N1: f5/spark reachable по факту ответа, не по 2xx ----------------------
def test_probe_reachable_on_404():
    # Сервер без эндпоинта /health отвечает 404 — значит порт слушает, процесс жив.
    c = _FakeClient(lambda url, h: _Resp(404))
    out = asyncio.run(tts._probe_responds(c, "http://f5:8991/tts"))
    assert out["reachable"] is True
    assert out["status"] == 404


def test_probe_reachable_on_200():
    c = _FakeClient(lambda url, h: _Resp(200))
    out = asyncio.run(tts._probe_responds(c, "http://f5:8810/tts"))
    assert out == {"reachable": True, "status": 200}


def test_probe_unreachable_on_transport_error():
    def boom(url, h):
        raise ConnectionError("Connection refused")

    out = asyncio.run(tts._probe_responds(_FakeClient(boom), "http://f5:8810/tts"))
    assert out["reachable"] is False
    assert "refused" in out["error"].lower()


# --- N4: ElevenLabs reachable по 2xx, с кэшем -------------------------------
def _set_eleven_creds(monkeypatch, key="k", voice="v"):
    monkeypatch.setattr(tts.settings, "elevenlabs_api_key", key)
    monkeypatch.setattr(tts.settings, "elevenlabs_voice_id", voice)
    monkeypatch.setattr(tts, "_eleven_health_cache", None)


def test_eleven_reachable_true_on_2xx(monkeypatch):
    _set_eleven_creds(monkeypatch)
    out = asyncio.run(tts._eleven_reachable(_FakeClient(lambda url, h: _Resp(200))))
    assert out["reachable"] is True


def test_eleven_unreachable_on_401(monkeypatch):
    _set_eleven_creds(monkeypatch)
    out = asyncio.run(tts._eleven_reachable(_FakeClient(lambda url, h: _Resp(401))))
    assert out["reachable"] is False
    assert "401" in out["error"]


def test_eleven_unreachable_without_credentials(monkeypatch):
    _set_eleven_creds(monkeypatch, key="", voice="")
    # Обработчик не должен вызваться — креды пусты.
    out = asyncio.run(tts._eleven_reachable(_FakeClient(lambda url, h: _Resp(200))))
    assert out["reachable"] is False


def test_eleven_result_cached(monkeypatch):
    _set_eleven_creds(monkeypatch)
    calls = {"n": 0}

    def handler(url, h):
        calls["n"] += 1
        return _Resp(200)

    c = _FakeClient(handler)
    asyncio.run(tts._eleven_reachable(c))
    asyncio.run(tts._eleven_reachable(c))
    assert calls["n"] == 1  # второй вызов взят из кэша, в сеть не ходил


# --- Агрегация healthy() ----------------------------------------------------
def test_healthy_aggregates_f5_and_eleven(monkeypatch):
    monkeypatch.setattr(tts.settings, "tts_provider", "f5")
    monkeypatch.setattr(tts.settings, "tts_kk_provider", "eleven")
    monkeypatch.setattr(tts.settings, "f5_url", "http://f5:8991/tts")

    async def fake_probe(client, url):
        return {"reachable": True, "status": 404}

    async def fake_eleven(client):
        return {"reachable": True}

    monkeypatch.setattr(tts, "_probe_responds", fake_probe)
    monkeypatch.setattr(tts, "_eleven_reachable", fake_eleven)

    out = asyncio.run(tts.healthy())
    assert out["f5"]["reachable"] is True     # N1: F5 жив, хоть и 404
    assert out["eleven"]["reachable"] is True  # N4: облако мониторится
    assert "spark" not in out                  # spark не задействован
