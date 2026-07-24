"""Логирование взаимодействий: /voice пишет строку JSONL, приватность вопроса
(full/hash/off), ответ по флагу, путь ошибки. Офлайн — клиенты замоканы."""
import json
import os

os.environ["STT_KK_USE_WHISPER"] = "false"
os.environ["STT_CORRECTION"] = "false"

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app import logging_setup, service
from app.clients import rag, stt, tts
from tests.util import wav_bytes


def _mock_pipeline(monkeypatch):
    async def fake_transcribe(audio, filename, language=None, content_type="audio/wav"):
        return "Какой порог по операциям с ювелирными изделиями"

    async def fake_ask(question, language=None, with_sources=True):
        return {"answer": "Порог пять миллионов тенге, статья 4 Закона о ПОД/ФТ.", "sources": ""}

    async def fake_healthy():
        return {"reachable": True}

    async def fake_synth(text, language=None):
        return wav_bytes()

    async def fake_correct(text, language=None):
        return text

    monkeypatch.setattr(stt, "transcribe", fake_transcribe)
    monkeypatch.setattr(rag, "ask", fake_ask)
    monkeypatch.setattr(rag, "healthy", fake_healthy)
    monkeypatch.setattr(tts, "synthesize", fake_synth)
    monkeypatch.setattr(service, "correct_transcript", fake_correct)


def _read_jsonl(log_dir: str) -> list[dict]:
    path = os.path.join(log_dir, "interactions.jsonl")
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


@pytest.fixture
def client(tmp_path, monkeypatch):
    """TestClient с включённой аналитикой во временный каталог."""
    monkeypatch.setattr(main.settings, "log_dir", str(tmp_path))
    monkeypatch.setattr(main.settings, "log_analytics", True)
    monkeypatch.setattr(main.settings, "log_questions", "full")
    monkeypatch.setattr(main.settings, "log_answers", True)
    _mock_pipeline(monkeypatch)
    with TestClient(main.app) as c:      # lifespan -> configure_logging() на tmp_path
        c._log_dir = str(tmp_path)       # чтобы тест знал, где файл
        yield c
    # отцепляем файловый хендлер, чтобы tmp_path удалился без открытого файла
    for h in list(logging_setup.analytics.handlers):
        logging_setup.analytics.removeHandler(h)
        h.close()


def _post_voice(client):
    files = {"data": ("a.wav", wav_bytes(), "audio/wav")}
    return client.post("/voice", files=files, data={"language": "russian"})


def test_voice_writes_one_jsonl_row(client):
    r = _post_voice(client)
    assert r.status_code == 200
    rows = _read_jsonl(client._log_dir)
    assert len(rows) == 1
    rec = rows[0]
    # содержание
    assert "ювелир" in rec["question"]                  # full: полный текст вопроса
    assert "пять миллионов" in rec["answer"]            # ответ пишется (log_answers=True)
    assert rec["answer_found"] is True
    assert rec["error"] is None
    assert rec["lang"] == "russian"
    assert rec["provider"] in ("f5", "spark", "eleven", "say")
    # тайминги — целые миллисекунды
    for k in ("stt_ms", "rag_ms", "tts_ms", "total_ms"):
        assert isinstance(rec[k], int) and rec[k] >= 0
    assert len(rec["id"]) == 8                          # request-id


def test_questions_off_omits_text(client):
    client.app  # noqa — фикстура уже подняла клиента
    main.settings.log_questions = "off"
    r = _post_voice(client)
    assert r.status_code == 200
    rec = _read_jsonl(client._log_dir)[0]
    assert rec["question"] is None                      # текст не сохранён
    assert rec["lang"] == "russian"                     # метрики остались
    assert rec["answer_found"] is True


def test_questions_hash_mode(client):
    main.settings.log_questions = "hash"
    r = _post_voice(client)
    assert r.status_code == 200
    rec = _read_jsonl(client._log_dir)[0]
    assert rec["question"].startswith("sha256:")
    assert "ювелир" not in rec["question"]


def test_answers_off_omits_answer(client):
    main.settings.log_answers = False
    r = _post_voice(client)
    assert r.status_code == 200
    rec = _read_jsonl(client._log_dir)[0]
    assert "answer" not in rec                           # ответ не пишется


def test_error_path_is_recorded(client, monkeypatch):
    async def boom_ask(question, language=None, with_sources=False):
        raise RuntimeError("RAG упал")

    monkeypatch.setattr(rag, "ask", boom_ask)
    r = _post_voice(client)
    assert r.status_code == 502
    rec = _read_jsonl(client._log_dir)[0]
    assert rec["error"] == "rag"
    assert rec["question"]                               # STT успел, вопрос есть
    assert isinstance(rec["stt_ms"], int)               # стадия STT замерена
    assert rec["rag_ms"] is None                         # RAG не дошёл до замера
    assert isinstance(rec["total_ms"], int)             # общий тайминг записан всегда
