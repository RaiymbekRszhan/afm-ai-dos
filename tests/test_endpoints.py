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


def test_voice_suggests_on_not_found(client, monkeypatch):
    """STT исказил вопрос, RAG не нашёл ответа -> в X-Suggest уходит исправленная
    формулировка, а озвучивается фраза-уточнение «возможно, вы хотели спросить…»."""
    import urllib.parse

    from app.clients import rag
    from app import service

    async def not_found_ask(question, language=None, with_sources=True):
        return {"answer": "К сожалению, по этому вопросу у меня нет точной "
                          "информации в базе Агентства.", "sources": ""}

    async def fake_suggest(question, language=None):
        return "Какие штрафы за неуплату налогов?"

    monkeypatch.setattr(rag, "ask", not_found_ask)
    monkeypatch.setattr(service, "suggest_question", fake_suggest)
    files = {"data": ("a.wav", wav_bytes(), "audio/wav")}
    r = client.post("/voice", files=files, data={"language": "russian"})
    assert r.status_code == 200
    assert urllib.parse.unquote(r.headers["X-Suggest"]) == "Какие штрафы за неуплату налогов?"
    assert "хотели спросить" in urllib.parse.unquote(r.headers["X-Answer"])


def test_voice_affirmative_answers_suggested(client, monkeypatch):
    """Реплика «да» + поле suggest -> отвечаем на ИСПРАВЛЕННЫЙ вопрос, без
    повторного уточнения (X-Suggest в ответе нет)."""
    import urllib.parse

    from app.clients import rag, stt

    asked = {}

    async def yes_transcribe(audio, filename, language=None, content_type="audio/wav"):
        return "Да."

    async def capture_ask(question, language=None, with_sources=True):
        asked["q"] = question
        return {"answer": "Штраф составляет 40 МРП по статье 214 КоАП.", "sources": ""}

    monkeypatch.setattr(stt, "transcribe", yes_transcribe)
    monkeypatch.setattr(rag, "ask", capture_ask)
    files = {"data": ("a.wav", wav_bytes(), "audio/wav")}
    r = client.post("/voice", files=files, data={
        "language": "russian", "suggest": "Какие штрафы за неуплату налогов?"})
    assert r.status_code == 200
    assert asked["q"] == "Какие штрафы за неуплату налогов?"
    assert urllib.parse.unquote(r.headers["X-Question"]) == "Какие штрафы за неуплату налогов?"
    assert "X-Suggest" not in r.headers


def test_voice_upload_too_large(client, monkeypatch):
    import app.main as main
    monkeypatch.setattr(main.settings, "max_upload_mb", 0)   # любой файл превысит лимит
    files = {"data": ("a.wav", wav_bytes(), "audio/wav")}
    r = client.post("/voice", files=files, data={"language": "russian"})
    assert r.status_code == 413
