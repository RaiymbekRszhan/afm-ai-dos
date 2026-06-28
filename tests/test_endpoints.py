"""Эндпоинты оркестратора на замоканных внешних сервисах (см. conftest)."""
from tests.util import wav_bytes


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["rag"]["reachable"] is True       # пинг RAG-сервиса
    assert "tts" in body and "enabled" in body["tts"]


def test_chat_ok(client):
    r = client.post("/chat", json={"question": "Какой порог по ювелирке?", "language": "russian"})
    assert r.status_code == 200
    body = r.json()
    assert "пять миллионов" in body["answer"]
    assert isinstance(body["sources"], str) and body["sources"]   # источники строкой


def test_chat_empty_question(client):
    r = client.post("/chat", json={"question": "   "})
    assert r.status_code == 400


def test_speak(client):
    r = client.post("/speak", json={"text": "Здравствуйте", "language": "russian"})
    assert r.status_code == 200
    assert r.headers["content-type"] == "audio/wav"
    assert r.content[:4] == b"RIFF"               # валидный WAV


def test_transcribe(client):
    files = {"data": ("a.wav", wav_bytes(), "audio/wav")}
    r = client.post("/transcribe", files=files, data={"language": "russian"})
    assert r.status_code == 200
    assert r.json()["text"]


def test_voice_returns_audio(client):
    files = {"data": ("a.wav", wav_bytes(), "audio/wav")}
    r = client.post("/voice", files=files, data={"language": "russian"})
    assert r.status_code == 200
    assert r.headers["content-type"] == "audio/wav"
    assert "X-Question" in r.headers           # percent-encoded распознанный вопрос
    assert "X-Answer" in r.headers             # percent-encoded текст ответа (для UI)
    assert r.content[:4] == b"RIFF"


def test_voice_upload_too_large(client, monkeypatch):
    import app.main as main
    monkeypatch.setattr(main.settings, "max_upload_mb", 0)   # любой файл превысит лимит
    files = {"data": ("a.wav", wav_bytes(), "audio/wav")}
    r = client.post("/voice", files=files, data={"language": "russian"})
    assert r.status_code == 413
