"""Health-проба TTS (N1 + N4): сервер без /health считается живым по факту
HTTP-ответа (multipart-F5 на GPU отвечает 404, но ЖИВ), ElevenLabs — по 2xx с кэшем.
"""
import asyncio

import pytest

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
    monkeypatch.setattr(tts.settings, "tts_fallback", "")
    monkeypatch.setattr(tts.settings, "tts_kk_fallback", "")
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


def test_healthy_probes_fallback_provider(monkeypatch):
    """Фолбэк проверяется наравне с основным: страховку надо мониторить ДО того,
    как пропадёт интернет к eleven и она понадобится."""
    monkeypatch.setattr(tts.settings, "tts_provider", "f5")
    monkeypatch.setattr(tts.settings, "tts_kk_provider", "eleven")
    monkeypatch.setattr(tts.settings, "tts_kk_fallback", "spark")
    monkeypatch.setattr(tts.settings, "f5_url", "http://f5:8991/tts")
    monkeypatch.setattr(tts.settings, "spark_url", "http://spark:8992/tts")

    async def fake_probe(client, url):
        return {"reachable": True, "status": 200}

    async def fake_eleven(client):
        return {"reachable": True}

    monkeypatch.setattr(tts, "_probe_responds", fake_probe)
    monkeypatch.setattr(tts, "_eleven_reachable", fake_eleven)

    out = asyncio.run(tts.healthy())
    assert out["spark"]["reachable"] is True


def test_healthy_probes_omni(monkeypatch):
    """Казахский на OmniVoice: пингуем его сервер, а не Spark (тот может вообще
    не быть поднят — это уже не основной движок)."""
    monkeypatch.setattr(tts.settings, "tts_provider", "f5")
    monkeypatch.setattr(tts.settings, "tts_kk_provider", "omni")
    monkeypatch.setattr(tts.settings, "tts_fallback", "")
    monkeypatch.setattr(tts.settings, "tts_kk_fallback", "")
    monkeypatch.setattr(tts.settings, "f5_url", "http://f5:8991/tts")
    monkeypatch.setattr(tts.settings, "omni_url", "http://omni:8993/tts")

    seen = []

    async def fake_probe(client, url):
        seen.append(url)
        return {"reachable": True, "status": 200}

    monkeypatch.setattr(tts, "_probe_responds", fake_probe)

    out = asyncio.run(tts.healthy())
    assert out["omni"]["reachable"] is True
    assert "spark" not in out
    assert "http://omni:8993/tts" in seen


# ---------------------------------------------------------------------------
# Проверка TLS у клиента ElevenLabs. На сервере АФМ прокси вскрывает TLS, и
# правку `verify=False` держали ПРЯМО В КОДЕ — она терялась при каждом
# обновлении и уносила с собой казахский голос. Теперь это настройка, и её
# поведение закреплено тестом.
# ---------------------------------------------------------------------------

def test_eleven_verify_ssl_passed_to_client(monkeypatch):
    seen = {}

    class _Client:
        def __init__(self, **kw):
            seen.update(kw)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, **kw):
            class _R:
                status_code = 200
                content = b"\x00\x00" * 100      # PCM: _eleven сам завернёт в WAV
                headers = {"content-type": "audio/mpeg"}

                @staticmethod
                def raise_for_status():
                    return None
            return _R()

    monkeypatch.setattr(tts.settings, "elevenlabs_api_key", "k")
    monkeypatch.setattr(tts.settings, "elevenlabs_voice_id", "v")
    # httpx в tts.py импортируется ВНУТРИ функций (ленивый импорт), поэтому
    # подменять надо сам модуль, а не атрибут tts.httpx — его нет.
    monkeypatch.setattr("httpx.AsyncClient", _Client)

    monkeypatch.setattr(tts.settings, "elevenlabs_verify_ssl", True)
    asyncio.run(tts._eleven("тест", "russian"))
    assert seen["verify"] is True                    # по умолчанию проверяем

    seen.clear()
    monkeypatch.setattr(tts.settings, "elevenlabs_verify_ssl", False)
    asyncio.run(tts._eleven("тест", "russian"))
    assert seen["verify"] is False                   # за прокси АФМ — отключаемо


# ---------------------------------------------------------------------------
# Контракт клиента OmniVoice. Он намеренно совпадает со Spark: смена движка
# должна стоить одной строки в .env, а не правки пайплайна.
# ---------------------------------------------------------------------------

def test_omni_posts_spark_contract(monkeypatch):
    seen = {}

    class _Client:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, **kw):
            seen["url"] = url
            seen["json"] = kw.get("json")

            class _R:
                content = b"RIFF....WAVE"

                @staticmethod
                def raise_for_status():
                    return None
            return _R()

    monkeypatch.setattr(tts.settings, "omni_url", "http://omni:8993/tts")
    monkeypatch.setattr("httpx.AsyncClient", _Client)

    out = asyncio.run(tts._omni("Сәлеметсіз бе.", "kazakh"))
    assert out == b"RIFF....WAVE"
    assert seen["url"] == "http://omni:8993/tts"
    assert seen["json"] == {"text": "Сәлеметсіз бе.", "language": "kazakh"}


def test_omni_without_url_raises(monkeypatch):
    # Без адреса — понятная ошибка, а не таймаут на пустой строке.
    monkeypatch.setattr(tts.settings, "omni_url", "")
    with pytest.raises(RuntimeError, match="OMNI_URL"):
        asyncio.run(tts._omni("тест", "kazakh"))
