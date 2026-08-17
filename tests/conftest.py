"""Общая настройка тестов: офлайн-конфиг + замоканные внешние клиенты.

Тесты НЕ ходят в сеть и НЕ грузят Whisper — внешние сервисы (STT-сервер АФМ,
RAG :8077, F5/OmniVoice/Spark) подменяются заглушками на уровне функций-клиентов.
"""
import os

# Должно стоять ДО импорта app.config (Settings читает окружение при импорте).
os.environ["STT_KK_USE_WHISPER"] = "false"   # не грузим Whisper в lifespan
os.environ["STT_CORRECTION"] = "false"        # не дёргаем LLM-коррекцию
os.environ["LOG_ANALYTICS"] = "false"         # обычные тесты не пишут JSONL/logs/ (см. test_logging)
# Лимит темпа выключен: общий клиент ходит с одного адреса и без id, поэтому
# все запросы суиты попадали бы в одно окно и после десятого получали 429.
# Тесты лимита включают его сами (см. test_kiosk_gate).
os.environ["KIOSK_RATE_PER_MIN"] = "0"
# f5/spark теперь требуют адрес (N3), иначе tts_enabled=False и /voice отдаёт JSON
# без звука. Задаём фиктивные URL — сам синтез в тестах замокан (см. fixture client).
os.environ.setdefault("F5_URL", "http://f5.test:8810/tts")
os.environ.setdefault("SPARK_URL", "http://spark.test:8809/tts")
os.environ.setdefault("OMNI_URL", "http://omni.test:8811/tts")

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app import analytics, kiosks, service
from app.clients import rag, stt, tts
from tests.util import wav_bytes


@pytest.fixture(autouse=True)
def _clean_fleet_state():
    """Состояние флота живёт в модуле, а не в приложении: без сброса живость
    точек и накопленный темп протекают из теста в тест."""
    kiosks.reset_cache()
    kiosks.reset_seen()
    analytics.reset_cache()
    yield
    kiosks.reset_cache()
    kiosks.reset_seen()
    analytics.reset_cache()


@pytest.fixture
def client(monkeypatch):
    """TestClient с замоканными STT / RAG / TTS — быстрый и офлайн."""

    async def fake_transcribe(audio, filename, language=None, content_type="audio/wav"):
        return "Какой порог по операциям с ювелирными изделиями"

    async def fake_ask(question, language=None, with_sources=True):
        return {
            "answer": "Порог пять миллионов тенге, согласно статье 4 Закона о ПОД/ФТ.",
            "sources": "ФТ, статья 4" if with_sources else "",
        }

    async def fake_healthy():
        return {"reachable": True, "status": "ok"}

    async def fake_synthesize(text, language=None):
        return wav_bytes()

    async def fake_synthesize_with_provider(text, language=None):
        # /voice берёт ФАКТИЧЕСКИЙ провайдер отсюда (после возможного фолбэка).
        return wav_bytes(), tts._provider_for(language)

    async def fake_tts_healthy():
        # Детальную health-пробу TTS проверяет tests/test_tts_health.py;
        # эндпоинт-тесты остаются офлайн (без httpx к f5.test/spark.test).
        return {"f5": {"reachable": True, "status": 200}}

    async def fake_correct(text, language=None):
        return text

    monkeypatch.setattr(stt, "transcribe", fake_transcribe)
    monkeypatch.setattr(rag, "ask", fake_ask)
    monkeypatch.setattr(rag, "healthy", fake_healthy)
    monkeypatch.setattr(tts, "synthesize", fake_synthesize)
    monkeypatch.setattr(tts, "synthesize_with_provider", fake_synthesize_with_provider)
    monkeypatch.setattr(tts, "healthy", fake_tts_healthy)
    monkeypatch.setattr(service, "correct_transcript", fake_correct)

    with TestClient(main.app) as c:
        yield c
