"""Общая настройка тестов: офлайн-конфиг + замоканные внешние клиенты.

Тесты НЕ ходят в сеть и НЕ грузят Whisper — внешние сервисы (STT-сервер АФМ,
RAG :8077, F5/Spark) подменяются заглушками на уровне функций-клиентов.
"""
import os

# Должно стоять ДО импорта app.config (Settings читает окружение при импорте).
os.environ["STT_KK_USE_WHISPER"] = "false"   # не грузим Whisper в lifespan
os.environ["STT_CORRECTION"] = "false"        # не дёргаем LLM-коррекцию
os.environ["LOG_ANALYTICS"] = "false"         # обычные тесты не пишут JSONL/logs/ (см. test_logging)

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app import service
from app.clients import rag, stt, tts
from tests.util import wav_bytes


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

    async def fake_correct(text, language=None):
        return text

    monkeypatch.setattr(stt, "transcribe", fake_transcribe)
    monkeypatch.setattr(rag, "ask", fake_ask)
    monkeypatch.setattr(rag, "healthy", fake_healthy)
    monkeypatch.setattr(tts, "synthesize", fake_synthesize)
    monkeypatch.setattr(service, "correct_transcript", fake_correct)

    with TestClient(main.app) as c:
        yield c
