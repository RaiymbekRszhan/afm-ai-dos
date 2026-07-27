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


def _decode_audio(body: dict) -> bytes:
    import base64
    return base64.b64decode(body["audio_b64"])


def test_voice_returns_json_with_audio_and_text(client):
    """N5: /voice отдаёт JSON-тело — вопрос/ответ + аудио в base64, без X-* заголовков."""
    files = {"data": ("a.wav", wav_bytes(), "audio/wav")}
    r = client.post("/voice", files=files, data={"language": "russian"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json")
    body = r.json()
    assert body["question"] and body["answer"]
    assert body["provider"]                        # активный TTS-провайдер (для темпа)
    assert _decode_audio(body)[:4] == b"RIFF"      # base64 -> валидный WAV
    # текст больше НЕ в заголовках
    assert "X-Answer" not in r.headers and "X-Question" not in r.headers


def test_voice_long_answer_survives_in_body(client, monkeypatch):
    """N5: длинный табличный ответ (превысил бы лимит HTTP-заголовка) доходит ЦЕЛИКОМ."""
    from app.clients import rag

    long_answer = "Ответ по статье 214. " * 2000  # ~42000 символов, кириллица

    async def long_ask(question, language=None, with_sources=True):
        return {"answer": long_answer, "sources": ""}

    monkeypatch.setattr(rag, "ask", long_ask)
    files = {"data": ("a.wav", wav_bytes(), "audio/wav")}
    r = client.post("/voice", files=files, data={"language": "russian"})
    assert r.status_code == 200
    assert r.json()["answer"] == long_answer       # ничего не потеряно и не обрезано


def test_voice_suggests_on_not_found(client, monkeypatch):
    """STT исказил вопрос, RAG не нашёл ответа -> в поле suggest уходит исправленная
    формулировка, а озвучивается фраза-уточнение «возможно, вы хотели спросить…»."""
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
    body = r.json()
    assert body["suggest"] == "Какие штрафы за неуплату налогов?"
    assert "хотели спросить" in body["answer"]


def test_voice_affirmative_answers_suggested(client, monkeypatch):
    """Реплика «да» + поле suggest -> отвечаем на ИСПРАВЛЕННЫЙ вопрос, без
    повторного уточнения (suggest в ответе — null)."""
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
    body = r.json()
    assert asked["q"] == "Какие штрафы за неуплату налогов?"
    assert body["question"] == "Какие штрафы за неуплату налогов?"
    assert body["suggest"] is None


def test_voice_no_print_offer_on_not_found(client, monkeypatch):
    """Фраза-отказ сама содержит «подать обращение через e-Otinish», но печатать
    бланк на неизвестный/off-topic вопрос НЕ предлагаем (иначе бланк лез бы на любой
    отказ — напр. про «субъектов Аргентины»). Регрессия демо 2026-07-27."""
    from app.clients import rag
    from app import service

    async def not_found_ask(question, language=None, with_sources=True):
        return {"answer": "К сожалению, по этому вопросу у меня нет точной информации "
                          "в базе Агентства. Также можно подать обращение через "
                          "платформу e-Otinish.", "sources": ""}

    async def no_suggest(question, language=None):
        return None

    monkeypatch.setattr(rag, "ask", not_found_ask)
    monkeypatch.setattr(service, "suggest_question", no_suggest)
    files = {"data": ("a.wav", wav_bytes(), "audio/wav")}
    r = client.post("/voice", files=files, data={"language": "russian"})
    assert r.status_code == 200
    body = r.json()
    assert body["print"] == []                          # кнопки печати нет
    assert "распечатать" not in body["answer"]          # приглашение не дописано


def test_voice_print_offer_on_real_application_answer(client, monkeypatch):
    """Контроль: на РЕАЛЬНЫЙ ответ про подачу жалобы печать образца по-прежнему
    предлагается (print + приглашение в answer) — фикс не должен это сломать."""
    from app.clients import rag

    async def app_ask(question, language=None, with_sources=True):
        return {"answer": "Чтобы подать жалобу, обратитесь в Агентство или подайте "
                          "обращение через платформу e-Otinish.", "sources": ""}

    monkeypatch.setattr(rag, "ask", app_ask)
    files = {"data": ("a.wav", wav_bytes(), "audio/wav")}
    r = client.post("/voice", files=files, data={"language": "russian"})
    assert r.status_code == 200
    body = r.json()
    assert body["print"]                                # кнопка печати есть
    assert "распечатать" in body["answer"]              # приглашение дописано


def test_voice_upload_too_large(client, monkeypatch):
    import app.main as main
    monkeypatch.setattr(main.settings, "max_upload_mb", 0)   # любой файл превысит лимит
    files = {"data": ("a.wav", wav_bytes(), "audio/wav")}
    r = client.post("/voice", files=files, data={"language": "russian"})
    assert r.status_code == 413
