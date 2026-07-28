"""Потоковый `/voice/stream`: текст сразу, озвучка кусками по мере синтеза.

Смысл эндпоинта — воспринимаемая задержка: гражданин слышит первый кусок, не
дожидаясь полного синтеза (замер 27.07: ответ на 1000 символов = ~6.5 с на F5,
~20 с на Spark). Здесь проверяется контракт потока, а не звук.
"""
import asyncio
import base64
import json

import pytest

from app.clients import tts
from tests.util import wav_bytes, wav_nframes


def _lines(resp) -> list[dict]:
    return [json.loads(line) for line in resp.text.splitlines() if line.strip()]


@pytest.fixture
def stream_client(client, monkeypatch):
    """client из conftest + потоковый синтез, отдающий три коротких куска."""

    async def fake_stream(text, language=None):
        for seq in range(3):
            yield seq, wav_bytes(), tts._provider_for(language), 100

    monkeypatch.setattr(tts, "synthesize_stream", fake_stream)
    return client


def _post(c, lang="russian"):
    return c.post("/voice/stream", files={"data": ("q.wav", b"RIFFfake", "audio/wav")},
                  data={"language": lang})


def test_stream_sends_text_first_then_audio_chunks(stream_client):
    r = _post(stream_client)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/x-ndjson")
    events = _lines(r)

    # Первым идёт текст: экран заполняется ДО того, как появится звук.
    assert events[0]["type"] == "meta"
    assert events[0]["question"] and events[0]["answer"]
    assert events[0]["format"] == "wav"
    audio = [e for e in events if e["type"] == "audio"]
    assert [e["seq"] for e in audio] == [0, 1, 2]
    assert all(base64.b64decode(e["audio_b64"]) == wav_bytes() for e in audio)
    assert events[-1]["type"] == "end"


def test_stream_reports_actual_provider_per_chunk(stream_client, monkeypatch):
    """Провайдер приходит с каждым куском: после фолбэка он не тот, что в настройках,
    а киоск по нему выбирает темп проигрывания."""
    async def fallback_stream(text, language=None):
        yield 0, wav_bytes(), "spark", 100

    monkeypatch.setattr(tts, "synthesize_stream", fallback_stream)
    events = _lines(_post(stream_client, lang="kazakh"))
    assert [e for e in events if e["type"] == "audio"][0]["provider"] == "spark"


def test_stream_reports_synthesis_failure_as_event(stream_client, monkeypatch):
    """Сбой синтеза после начала потока = строка error (статус уже 200 отдан),
    текст ответа у гражданина на экране остаётся."""
    async def failing_stream(text, language=None):
        yield 0, wav_bytes(), "f5", 100
        raise ConnectionError("F5 отвалился на втором куске")

    monkeypatch.setattr(tts, "synthesize_stream", failing_stream)
    events = _lines(_post(stream_client))
    assert events[0]["type"] == "meta"
    assert events[1]["type"] == "audio"
    assert events[-1]["type"] == "error"
    assert "TTS" in events[-1]["detail"]


def test_stream_stt_error_is_plain_http_error(client, monkeypatch):
    """Ошибка ДО первого байта — обычный HTTP-код, а не строка потока."""
    async def boom(*a, **k):
        raise ConnectionError("STT недоступен")

    from app.clients import stt
    monkeypatch.setattr(stt, "transcribe", boom)
    r = _post(client)
    assert r.status_code == 502


def test_stream_releases_semaphore(stream_client):
    """Семафор TTS/GPU держится на весь поток и отпускается по его завершении —
    иначе второй запрос повис бы навсегда."""
    import app.main as main

    _post(stream_client)
    assert main._tts_sem._value == main.settings.max_concurrent_voice
    _post(stream_client)                      # второй проходит, значит не залип
    assert main._tts_sem._value == main.settings.max_concurrent_voice


def test_stream_releases_semaphore_on_pipeline_error(client, monkeypatch):
    import app.main as main
    from app.clients import rag

    async def boom(*a, **k):
        raise ConnectionError("RAG недоступен")

    monkeypatch.setattr(rag, "ask", boom)
    assert _post(client).status_code == 502
    assert main._tts_sem._value == main.settings.max_concurrent_voice


# ---------- сам генератор кусков ----------
def _collect(text, lang):
    async def run():
        return [item async for item in tts.synthesize_stream(text, lang)]
    return asyncio.run(run())


def test_synthesize_stream_bakes_pause_into_chunk_tail(monkeypatch):
    """Паузу на стыке в потоке некому вставлять при склейке — она вшита в хвост
    куска, кроме последнего (иначе следующий кусок «влезет» без паузы)."""
    monkeypatch.setattr(tts.settings, "tts_provider", "say")
    monkeypatch.setattr(tts.settings, "tts_fallback", "")
    monkeypatch.setattr(tts.settings, "tts_gap_ms", 300)
    fr = 16000

    async def fake_one(text, language=None, provider=None):
        return wav_bytes(seconds=0.05, framerate=fr)

    monkeypatch.setattr(tts, "_synthesize_one", fake_one)

    text = "Первое предложение. " * 30       # заведомо несколько кусков
    items = _collect(text, "russian")
    assert len(items) > 1
    base = 800                                # 0.05 c при 16 кГц
    for seq, audio, _provider, _chars in items[:-1]:
        assert wav_nframes(audio) == base + int(fr * 300 / 1000)   # +полная пауза
    assert wav_nframes(items[-1][1]) == base                       # последнему не нужна


def test_synthesize_stream_falls_back_before_first_chunk(monkeypatch):
    """Пока ничего не отдано, отказ движка ещё можно пережить сменой на запасной."""
    tts._provider_down.clear()
    monkeypatch.setattr(tts.settings, "tts_kk_provider", "eleven")
    monkeypatch.setattr(tts.settings, "tts_kk_fallback", "spark")
    monkeypatch.setattr(tts.settings, "tts_retries", 0)

    async def fake_one(text, language=None, provider=None):
        if provider == "eleven":
            raise ConnectionError("нет интернета")
        return wav_bytes()

    monkeypatch.setattr(tts, "_synthesize_one", fake_one)
    items = _collect("Сәлеметсіз бе.", "kazakh")
    assert items and all(item[2] == "spark" for item in items)
    tts._provider_down.clear()
